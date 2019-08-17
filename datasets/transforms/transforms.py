import random
import torch
from torch.nn.functional import pad
import PIL
import numpy as np
from . import functional as F
from torch.nn.functional import interpolate
from utils.metrics.metrics import bbox_iou
from torchvision.transforms import Compose


# All the input data in these transforms is a tuple consists of the image and the annotations.

class HorizontalFlip(object):
    def __init__(self, p=0.5):
        self.p = p

    def __call__(self, data):
        assert isinstance(data[0], torch.Tensor)
        if random.random() > self.p:
            return data
        else:
            w = data[0].size(2)
            return F.flip_img(data[0]), F.flip_annos(data[1], w)


class ToTensor(object):
    def __call__(self, data):
        return F.img_to_tensor(data[0]), F.annos_to_tensor(data[1])


class Normalize(object):
    def __init__(self, mean=(0, 0, 0), std=(1, 1, 1)):
        self.mean = mean
        self.std = std

    def __call__(self, data):
        assert isinstance(data[0], torch.Tensor)
        return F.normalize(data[0], self.mean, self.std), data[1]


class ResizeBySize(object):
    def __init__(self, size):
        self.size = size

    def __call__(self, data):
        data = F.resize_by_size(data, self.size)
        return data


class RandomCrop(object):
    def __init__(self, size, keep_iou=0.5):
        self.h, self.w = size
        self.keep_iou = keep_iou

    def generate_coor(self, img):
        h, w = img.size()[-2:]
        rx, ry = random.random() * (w - self.w), random.random() * (h - self.h)
        crop_coordinate = int(rx), int(ry), int(rx) + self.w, int(ry) + self.h
        return crop_coordinate

    def remove_bbox_outside(self, annos, xywh):
        _, overlap = bbox_iou(annos, xywh, x1y1x2y2=False, overlap=True)
        keep_flag = overlap.squeeze() > self.keep_iou
        annos = annos[keep_flag, :]
        annos = annos.view(-1, 8)
        return annos

    def __call__(self, data):
        assert isinstance(data[0], torch.Tensor)
        assert isinstance(data[1], torch.Tensor)

        img = data[0]
        h, w = img.size()[-2:]

        if (self.w, self.h) == (w, h):
            return data
        elif self.w > w and self.h > h:
            img = pad(img, [0, self.w - w, 0, self.h - h])
            return img, data[1]
        if self.w > w or self.h > h:
            img = pad(img, [0, max(self.w - w, 0), 0, max(self.h - h, 0)])

        h, w = img.size()[-2:]

        crop_coordinate = self.generate_coor(img)

        annos = data[1].clone()
        remove_large_flag = 1 - (((annos[:, 2] > self.w) + (annos[:, 3] > self.h)) > 0)
        annos_wo_l = annos[remove_large_flag, :]

        if annos_wo_l.size(0) == 0:
            # Means that current scale size is invalid.
            min_side = min(h, w)
            scale_factor = self.w / min_side
            resize_h, resize_w = int(h * scale_factor), int(w * scale_factor)
            img = interpolate(img.unsqueeze(0), size=(resize_h, resize_w), mode='bilinear',
                              align_corners=True).squeeze()
            annos_wo_l = data[1].clone()
            annos_wo_l[:, :4] = annos_wo_l[:, :4] * scale_factor
            crop_coordinate = self.generate_coor(img)

        annos = self.remove_bbox_outside(annos_wo_l,
                                         torch.tensor([[crop_coordinate[0], crop_coordinate[1], self.w, self.h]]))

        h, w = img.size()[-2:]
        if annos.size(0) == 0:
            rand_idx = torch.randint(0, annos_wo_l.size(0), (1,))
            include_bbox = annos_wo_l[rand_idx, :].squeeze()
            x1, y1, x2, y2 = include_bbox[0], include_bbox[1], \
                             include_bbox[0] + include_bbox[2], include_bbox[1] + include_bbox[3]
            max_x1 = min(int(x1), int(w - self.w))
            min_x1 = max(0, int(x2 - self.w))
            max_y1 = min(int(y1), int(h - self.h))
            min_y1 = max(0, int(y2 - self.h))
            min_x1, max_x1 = sorted([min_x1, max_x1])
            min_y1, max_y1 = sorted([min_y1, max_y1])
            x1 = np.random.randint(min_x1, max_x1) if min_x1 != max_x1 else min_x1
            y1 = np.random.randint(min_y1, max_y1) if min_y1 != max_y1 else min_y1
            crop_coordinate = (int(x1), int(y1), int(x1) + self.w, int(y1) + self.h)
            annos = self.remove_bbox_outside(annos_wo_l, torch.tensor([[x1, y1, self.w, self.h]]))

        cropped_annos = F.crop_annos(annos, crop_coordinate, self.h, self.w)
        cropped_img = F.crop_tensor(img, crop_coordinate)
        if cropped_annos.size(0) == 0:
            cropped_annos = torch.tensor([[0,0,1,1,1,0,-1,-1]])
            cropped_img = torch,zeros_like(img)
        return cropped_img, cropped_annos


class ColorJitter(object):
    def __init__(self, brightness=0.5, contrast=0.5, saturation=0.5):
        self.brightness = [max(1 - brightness, 0), 1 + brightness]
        self.contrast = [max(1 - contrast, 0), 1 + contrast]
        self.saturation = [max(1 - saturation, 0), 1 + saturation]

    def __call__(self, data):
        assert isinstance(data[0], PIL.Image.Image) or \
           isinstance(data[0], PIL.PngImagePlugin.PngImageFile) or \
           isinstance(data[0], PIL.JpegImagePlugin.JpegImageFile)
        return F.color_jitter(data[0], self.brightness, self.contrast, self.saturation), data[1]


class MultiScale(object):
    def __init__(self, scale=(0.5, 0.75, 1, 1.25, 1.5)):
        self.scale = scale

    def __call__(self, data):
        rand_idx = random.randint(0, len(self.scale) - 1)
        return F.resize(data, self.scale[rand_idx])


class ToHeatmap(object):
    def __init__(self, scale_factor=4, cls_num=4):
        self.scale_factor = scale_factor
        self.cls_num = cls_num

    def __call__(self, data):
        img, annos, hm, wh, ind, offset, reg_mask = F.to_heatmap(data, self.scale_factor, self.cls_num)
        return img, annos, hm, wh, ind, offset, reg_mask


class FillDuck(object):
    def __init__(self, cls_list=(1, 2, 3, 7, 8, 10), factor=0.00005):
        self.cls_list = torch.tensor(cls_list).unsqueeze(0)
        self.factor = factor

    def __call__(self, data):
        return F.fill_duck(data, self.cls_list, self.factor)


class WhiteBalance(object):
    def __init__(self):
        self.num = 0

    def __call__(self, data):
        self.num = random.randint(0, 1)
        return F.whitebalance(data, self.num)

