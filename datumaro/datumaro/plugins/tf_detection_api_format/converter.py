
# Copyright (C) 2019 Intel Corporation
#
# SPDX-License-Identifier: MIT

import codecs
from collections import OrderedDict
from itertools import groupby
import logging as log
import os
import os.path as osp
import string

from datumaro.components.extractor import (AnnotationType, DEFAULT_SUBSET_NAME,
    LabelCategories
)
from datumaro.components.converter import Converter
from datumaro.components.cli_plugin import CliPlugin
from datumaro.util.image import encode_image
from datumaro.util.mask_tools import merge_masks
from datumaro.util.tf_util import import_tf as _import_tf

from .format import DetectionApiPath
tf = _import_tf()


# filter out non-ASCII characters, otherwise training will crash
_printable = set(string.printable)
def _make_printable(s):
    return ''.join(filter(lambda x: x in _printable, s))

def int64_feature(value):
    return tf.train.Feature(int64_list=tf.train.Int64List(value=[value]))

def int64_list_feature(value):
    return tf.train.Feature(int64_list=tf.train.Int64List(value=value))

def bytes_feature(value):
    return tf.train.Feature(bytes_list=tf.train.BytesList(value=[value]))

def bytes_list_feature(value):
    return tf.train.Feature(bytes_list=tf.train.BytesList(value=value))

def float_list_feature(value):
    return tf.train.Feature(float_list=tf.train.FloatList(value=value))

class TfDetectionApiConverter(Converter, CliPlugin):
    @classmethod
    def build_cmdline_parser(cls, **kwargs):
        parser = super().build_cmdline_parser(**kwargs)
        parser.add_argument('--save-images', action='store_true',
            help="Save images (default: %(default)s)")
        parser.add_argument('--save-masks', action='store_true',
            help="Include instance masks (default: %(default)s)")
        return parser

    def __init__(self, save_images=False, save_masks=False):
        super().__init__()

        self._save_images = save_images
        self._save_masks = save_masks

    def __call__(self, extractor, save_dir):
        os.makedirs(save_dir, exist_ok=True)

        label_categories = extractor.categories().get(AnnotationType.label,
            LabelCategories())
        get_label = lambda label_id: label_categories.items[label_id].name \
            if label_id is not None else ''
        label_ids = OrderedDict((label.name, 1 + idx)
            for idx, label in enumerate(label_categories.items))
        map_label_id = lambda label_id: label_ids.get(get_label(label_id), 0)
        self._get_label = get_label
        self._get_label_id = map_label_id

        subsets = extractor.subsets()
        if len(subsets) == 0:
            subsets = [ None ]

        for subset_name in subsets:
            if subset_name:
                subset = extractor.get_subset(subset_name)
            else:
                subset_name = DEFAULT_SUBSET_NAME
                subset = extractor

            labelmap_path = osp.join(save_dir, DetectionApiPath.LABELMAP_FILE)
            with codecs.open(labelmap_path, 'w', encoding='utf8') as f:
                for label, idx in label_ids.items():
                    f.write(
                        'item {\n' +
                        ('\tid: %s\n' % (idx)) +
                        ("\tname: '%s'\n" % (label)) +
                        '}\n\n'
                    )

            anno_path = osp.join(save_dir, '%s.tfrecord' % (subset_name))
            with tf.io.TFRecordWriter(anno_path) as writer:
                for item in subset:
                    tf_example = self._make_tf_example(item)
                    writer.write(tf_example.SerializeToString())

    def _find_instance_parts(self, group, img_width, img_height):
        boxes = [a for a in group if a.type == AnnotationType.bbox]
        masks = [a for a in group if a.type == AnnotationType.mask]

        anns = boxes + masks
        leader = self.find_group_leader(anns)
        bbox = self._compute_bbox(anns)

        mask = None
        if self._save_masks:
            mask = merge_masks([m.image for m in masks])

        return [leader, mask, bbox]

    @staticmethod
    def find_group_leader(group):
        return max(group, key=lambda x: x.get_area())

    @staticmethod
    def _compute_bbox(annotations):
        boxes = [ann.get_bbox() for ann in annotations]
        x0 = min((b[0] for b in boxes), default=0)
        y0 = min((b[1] for b in boxes), default=0)
        x1 = max((b[0] + b[2] for b in boxes), default=0)
        y1 = max((b[1] + b[3] for b in boxes), default=0)
        return [x0, y0, x1 - x0, y1 - y0]

    @staticmethod
    def _find_instance_anns(annotations):
        return [a for a in annotations
            if a.type in { AnnotationType.bbox, AnnotationType.mask }
        ]

    @classmethod
    def _find_instances(cls, annotations):
        instance_anns = cls._find_instance_anns(annotations)

        ann_groups = []
        for g_id, group in groupby(instance_anns, lambda a: a.group):
            if not g_id:
                ann_groups.extend(([a] for a in group))
            else:
                ann_groups.append(list(group))

        return ann_groups

    def _export_instances(self, instances, width, height):
        xmins = [] # List of normalized left x coordinates of bounding boxes (1 per box)
        xmaxs = [] # List of normalized right x coordinates of bounding boxes (1 per box)
        ymins = [] # List of normalized top y coordinates of bounding boxes (1 per box)
        ymaxs = [] # List of normalized bottom y coordinates of bounding boxes (1 per box)
        classes_text = [] # List of class names of bounding boxes (1 per box)
        classes = [] # List of class ids of bounding boxes (1 per box)
        masks = [] # List of PNG-encoded instance masks (1 per box)

        for leader, mask, box in instances:
            label = _make_printable(self._get_label(leader.label))
            classes_text.append(label.encode('utf-8'))
            classes.append(self._get_label_id(leader.label))

            xmins.append(box[0] / width)
            xmaxs.append((box[0] + box[2]) / width)
            ymins.append(box[1] / height)
            ymaxs.append((box[1] + box[3]) / height)

            if self._save_masks:
                if mask is not None:
                    mask = encode_image(mask, '.png')
                else:
                    mask = b''
                masks.append(mask)

        result = {}
        if classes:
            result = {
                'image/object/bbox/xmin': float_list_feature(xmins),
                'image/object/bbox/xmax': float_list_feature(xmaxs),
                'image/object/bbox/ymin': float_list_feature(ymins),
                'image/object/bbox/ymax': float_list_feature(ymaxs),
                'image/object/class/text': bytes_list_feature(classes_text),
                'image/object/class/label': int64_list_feature(classes),
            }
            if masks:
                result['image/object/mask'] = bytes_list_feature(masks)
        return result

    def _make_tf_example(self, item):
        features = {
            'image/source_id': bytes_feature(str(item.id).encode('utf-8')),
            'image/filename': bytes_feature(
                ('%s%s' % (item.id, DetectionApiPath.IMAGE_EXT)).encode('utf-8')),
        }

        if not item.has_image:
            raise Exception("Failed to export dataset item '%s': "
                "item has no image info" % item.id)
        height, width = item.image.size

        features.update({
            'image/height': int64_feature(height),
            'image/width': int64_feature(width),
        })

        features.update({
            'image/encoded': bytes_feature(b''),
            'image/format': bytes_feature(b'')
        })
        if self._save_images:
            if item.has_image and item.image.has_data:
                fmt = DetectionApiPath.IMAGE_FORMAT
                buffer = encode_image(item.image.data, DetectionApiPath.IMAGE_EXT)

                features.update({
                    'image/encoded': bytes_feature(buffer),
                    'image/format': bytes_feature(fmt.encode('utf-8')),
                })
            else:
                log.warning("Item '%s' has no image" % item.id)

        instances = self._find_instances(item.annotations)
        instances = [self._find_instance_parts(i, width, height) for i in instances]
        features.update(self._export_instances(instances, width, height))

        tf_example = tf.train.Example(
            features=tf.train.Features(feature=features))

        return tf_example
