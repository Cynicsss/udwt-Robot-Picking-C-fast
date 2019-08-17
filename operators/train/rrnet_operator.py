import os
import random
from models.rrnet import RRNet
from modules.loss.focalloss import FocalLossHM
import numpy as np
from modules.loss.regl1loss import RegL1Loss
import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
import torchvision
from torch.nn.parallel import DistributedDataParallel

from datasets import make_train_dataloader
from utils.vis.logger import Logger
from datasets.transforms.functional import denormalize
from utils.vis.annotations import visualize
from ext.nms.nms_wrapper import nms


class RRNetTrainOperator(object):
    def __init__(self, cfg):
        self.cfg = cfg

        random.seed(cfg.seed)
        torch.manual_seed(cfg.seed)
        torch.cuda.manual_seed(cfg.seed)

        model = RRNet(cfg).cuda(cfg.Distributed.gpu_id)
        model = nn.SyncBatchNorm.convert_sync_batchnorm(model)
        self.optimizer = optim.Adam(model.parameters(), lr=cfg.Train.lr)
        self.lr_sch = optim.lr_scheduler.MultiStepLR(self.optimizer, milestones=cfg.Train.lr_milestones, gamma=0.1)
        self.training_loader = make_train_dataloader(cfg)

        self.model = DistributedDataParallel(model, find_unused_parameters=True, device_ids=[self.cfg.Distributed.gpu_id])

        self.hm_focal_loss = FocalLossHM()
        self.l1_loss = RegL1Loss()

        self.main_proc_flag = cfg.Distributed.gpu_id == 0

    def criterion(self, outs, targets):
        s1_hms, s1_whs, s1_offsets, s2_reg, bxyxy, scores, _ = outs
        gt_hms, gt_whs, gt_inds, gt_offsets, gt_reg_masks, gt_annos = targets
        bs = s1_hms.size(0)

        # I. Stage 1
        s1_hm = s1_hms
        s1_wh = s1_whs
        s1_offset = s1_offsets
        s1_hm = torch.clamp(torch.sigmoid(s1_hm), min=1e-4, max=1-1e-4)
        # Heatmap Loss
        hm_loss = self.hm_focal_loss(s1_hm, gt_hms)
        # WH Loss
        wh_loss = self.l1_loss(s1_wh, gt_reg_masks, gt_inds, gt_whs)
        # OffSet Loss
        off_loss = self.l1_loss(s1_offset, gt_reg_masks, gt_inds, gt_offsets)

        # II. Stage2 Loss
        s2_reg_loss = 0
        # Calculate IOU between prediction and bbox
        # 1. Transform bbox.
        gt_annos[:, :, 2:4] += gt_annos[:, :, 0:2]
        for b_idx in range(bs):
            batch_flag = bxyxy[:, 0] == b_idx
            bbox = bxyxy[batch_flag][:, 1:]
            gt_anno = gt_annos[b_idx]
            iou = torchvision.ops.box_iou(bbox*self.cfg.scale_factor, gt_anno[:, :4])
            max_iou, max_idx = torch.max(iou, dim=1)
            pos_idx = max_iou > 0.5
            # 2. Regression Loss
            if pos_idx.sum() == 0:
                pos_idx = torch.zeros_like(max_iou, device=max_iou.device).byte()
                pos_idx[0] = 1
                pos_factor = 0
            else:
                pos_factor = 1
            gt_reg = self.generate_bbox_target(bbox[pos_idx, :]*self.cfg.scale_factor, gt_anno[max_idx[pos_idx], :4])
            s2_reg_loss += F.smooth_l1_loss(s2_reg[batch_flag][pos_idx], gt_reg) * pos_factor / bs
        return hm_loss, wh_loss, off_loss, s2_reg_loss

    @staticmethod
    def generate_bbox_target(ex_rois, gt_rois):
        ex_widths = ex_rois[:, 2] - ex_rois[:, 0] + 1.0
        ex_heights = ex_rois[:, 3] - ex_rois[:, 1] + 1.0
        ex_ctr_x = ex_rois[:, 0] + 0.5 * ex_widths
        ex_ctr_y = ex_rois[:, 1] + 0.5 * ex_heights

        gt_widths = gt_rois[:, 2] - gt_rois[:, 0] + 1.0
        gt_heights = gt_rois[:, 3] - gt_rois[:, 1] + 1.0
        gt_ctr_x = gt_rois[:, 0] + 0.5 * gt_widths
        gt_ctr_y = gt_rois[:, 1] + 0.5 * gt_heights

        targets_dx = (gt_ctr_x - ex_ctr_x) / ex_widths
        targets_dy = (gt_ctr_y - ex_ctr_y) / ex_heights
        targets_dw = torch.log(gt_widths / ex_widths)
        targets_dh = torch.log(gt_heights / ex_heights)
        return torch.stack((targets_dx, targets_dy, targets_dw, targets_dh), dim=1)

    def training_process(self):
        if self.main_proc_flag:
            logger = Logger(self.cfg)

        self.model.train()

        total_loss = 0
        total_hm_loss = 0
        total_wh_loss = 0
        total_off_loss = 0
        total_s2_reg_loss = 0

        for step in range(self.cfg.Train.iter_num):
            self.lr_sch.step()
            self.optimizer.zero_grad()
            try:
                imgs, annos, gt_hms, gt_whs, gt_inds, gt_offsets, gt_reg_masks, names = self.training_loader.get_batch()
            except Exception as e:
                print(e)
                with open('./log/error.log', 'a+') as writer:
                    writer.write(str(e)+'\n')
            outs = self.model(imgs, k=100*4)

            targets = gt_hms, gt_whs, gt_inds, gt_offsets, gt_reg_masks, annos
            hm_loss, wh_loss, offset_loss, s2_reg_loss = self.criterion(outs, targets)

            if step < 500:
                s2_factor = 0
            else:
                s2_factor = 1
            loss = hm_loss + (0.1 * wh_loss) + offset_loss + s2_reg_loss*s2_factor
            loss.backward()
            self.optimizer.step()

            total_loss += float(loss)
            total_hm_loss += float(hm_loss)
            total_wh_loss += float(wh_loss)
            total_off_loss += float(offset_loss)
            total_s2_reg_loss += float(s2_reg_loss)

            if self.main_proc_flag:
                if step % self.cfg.Train.print_interval == self.cfg.Train.print_interval - 1:
                    # Loss
                    for param_group in self.optimizer.param_groups:
                        lr = param_group['lr']
                    log_data = {'scalar': {
                        'train/total_loss': total_loss / self.cfg.Train.print_interval,
                        'train/hm_loss': total_hm_loss / self.cfg.Train.print_interval,
                        'train/wh_loss': total_wh_loss / self.cfg.Train.print_interval,
                        'train/off_loss': total_off_loss / self.cfg.Train.print_interval,
                        'train/s2_reg_loss': total_s2_reg_loss / self.cfg.Train.print_interval,
                        'train/lr': lr
                    }}

                    # Generate bboxs
                    s1_pred_bbox, s2_pred_bbox = self.generate_bbox(outs, batch_idx=0)

                    # Visualization
                    img = (denormalize(imgs[0].cpu(), mean=self.cfg.mean, std=self.cfg.std).permute(1, 2, 0).cpu().numpy() * 255).astype(np.uint8)
                    # Do nms
                    s2_pred_bbox = s2_pred_bbox[s2_pred_bbox[:, 4] > 0.1]
                    s2_pred_bbox = self._ext_nms(s2_pred_bbox)
                    #
                    s1_pred_on_img = visualize(img.copy(), s1_pred_bbox, xywh=True, with_score=True)
                    s2_pred_on_img = visualize(img.copy(), s2_pred_bbox, xywh=True, with_score=True)
                    gt_img = visualize(img.copy(), annos[0, :, :6], xywh=False)

                    s1_pred_on_img = torch.from_numpy(s1_pred_on_img).permute(2, 0, 1).unsqueeze(0).float() / 255.
                    s2_pred_on_img = torch.from_numpy(s2_pred_on_img).permute(2, 0, 1).unsqueeze(0).float() / 255.
                    gt_on_img = torch.from_numpy(gt_img).permute(2, 0, 1).unsqueeze(0).float() / 255.
                    log_data['imgs'] = {'Train': [s1_pred_on_img, s2_pred_on_img, gt_on_img]}
                    logger.log(log_data, step)

                    total_loss = 0
                    total_hm_loss = 0
                    total_wh_loss = 0
                    total_off_loss = 0
                    total_s2_reg_loss = 0

                if step % self.cfg.Train.checkpoint_interval == self.cfg.Train.checkpoint_interval - 1 or \
                        step == self.cfg.Train.iter_num - 1:
                    self.save_ckp(self.model.module, step, logger.log_dir)

    def generate_bbox(self, outs, batch_idx=0):
        s1_hms, s1_whs, s1_offsets, s2_reg, bxyxy, scores, clses = outs
        batch_flag = bxyxy[:, 0] == batch_idx
        s2_reg = s2_reg[batch_flag]
        xyxy = bxyxy[batch_flag]
        xyxy[:, 1:5] *= self.cfg.scale_factor
        score = scores[batch_flag]
        clses = clses[batch_flag]

        s1_xywh = xyxy[:, 1:5]
        s1_xywh[:, 2:4] -= s1_xywh[:, 0:2]
        s1_bboxes = torch.cat((s1_xywh, score.view(-1, 1), torch.zeros((s1_xywh.size(0), 1), device=xyxy.device)), dim=1)

        s2_xywh = s1_xywh
        s2_xywh[:, 2:4] += 1
        out_ctr_x = s2_reg[:, 0] * s2_xywh[:, 2] + s2_xywh[:, 0] + s2_xywh[:, 2] / 2
        out_ctr_y = s2_reg[:, 1] * s2_xywh[:, 3] + s2_xywh[:, 1] + s2_xywh[:, 3] / 2
        out_w = s2_reg[:, 2].exp() * s2_xywh[:, 2]
        out_h = s2_reg[:, 3].exp() * s2_xywh[:, 3]
        out_x = out_ctr_x - out_w / 2.
        out_y = out_ctr_y - out_h / 2.
        s2_bboxes = torch.stack((out_x, out_y, out_w, out_h, score, clses.float()+1), dim=1)
        return s1_bboxes, s2_bboxes

    @staticmethod
    def _ext_nms(pred_bbox, per_cls=True):
        if pred_bbox.size(0) == 0:
            return pred_bbox
        keep_bboxs = []
        if per_cls:
            cls_unique = pred_bbox[:, 5].unique()
            for cls in cls_unique:
                cls_idx = pred_bbox[:, 5] == cls
                bbox_for_nms = pred_bbox[cls_idx].detach().cpu().numpy()
                bbox_for_nms[:, 2] = bbox_for_nms[:, 0] + bbox_for_nms[:, 2]
                bbox_for_nms[:, 3] = bbox_for_nms[:, 1] + bbox_for_nms[:, 3]
                keep_bbox = nms(bbox_for_nms, thresh=0.3)
                keep_bboxs.append(keep_bbox)
            keep_bboxs = np.concatenate(keep_bboxs, axis=0)
        else:
            bbox_for_nms = pred_bbox.detach().cpu().numpy()
            bbox_for_nms[:, 2] = bbox_for_nms[:, 0] + bbox_for_nms[:, 2]
            bbox_for_nms[:, 3] = bbox_for_nms[:, 1] + bbox_for_nms[:, 3]
            keep_bboxs = nms(bbox_for_nms, thresh=0.3)
        keep_bboxs[:, 2:4] -= keep_bboxs[:, 0:2]
        return torch.from_numpy(keep_bboxs)

    @staticmethod
    def save_ckp(models, step, path):
        """
        Save checkpoint of the model.
        :param models: nn.Module
        :param step: step of the checkpoint.
        :param path: save path.
        """
        torch.save(models.state_dict(), os.path.join(path, 'ckp-{}.pth'.format(step)))