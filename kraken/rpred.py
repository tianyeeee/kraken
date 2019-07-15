# -*- coding: utf-8 -*-
#
# Copyright 2015 Benjamin Kiessling
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
# or implied. See the License for the specific language governing
# permissions and limitations under the License.
"""
kraken.rpred
~~~~~~~~~~~~

Generators for recognition on lines images.
"""
import logging
import numpy as np
import bidi.algorithm as bd

from PIL import Image, ImageDraw
from typing import List, Tuple, Optional, Generator, Union, Dict, Any

from scipy.spatial import distance_matrix
from scipy.spatial.distance import pdist, squareform
from skimage.transform import PiecewiseAffineTransform, warp

from kraken.lib.util import get_im_str
from kraken.lib.models import TorchSeqRecognizer
from kraken.lib.exceptions import KrakenInputException
from kraken.lib.dataset import generate_input_transforms


__all__ = ['ocr_record', 'bidi_record', 'mm_rpred', 'rpred']

logger = logging.getLogger(__name__)


class ocr_record(object):
    """
    A record object containing the recognition result of a single line
    """
    def __init__(self, prediction: str, cuts, confidences: List[float]) -> None:
        self.prediction = prediction
        self.cuts = cuts
        self.confidences = confidences

    def __len__(self) -> int:
        return len(self.prediction)

    def __str__(self) -> str:
        return self.prediction

    def __iter__(self):
        self.idx = -1
        return self

    def __next__(self) -> Tuple[str, int, float]:
        if self.idx + 1 < len(self):
            self.idx += 1
            return (self.prediction[self.idx], self.cuts[self.idx],
                    self.confidences[self.idx])
        else:
            raise StopIteration

    def __getitem__(self, key: Union[int, slice]):
        if isinstance(key, slice):
            return [self[i] for i in range(*key.indices(len(self)))]
        elif isinstance(key, int):
            if key < 0:
                key += len(self)
            if key >= len(self):
                raise IndexError('Index (%d) is out of range' % key)
            return (self.prediction[key], self.cuts[key],
                    self.confidences[key])
        else:
            raise TypeError('Invalid argument type')


def bidi_record(record: ocr_record) -> ocr_record:
    """
    Reorders a record using the Unicode BiDi algorithm.

    Models trained for RTL or mixed scripts still emit classes in LTR order
    requiring reordering for proper display.

    Args:
        record (kraken.rpred.ocr_record)

    Returns:
        kraken.rpred.ocr_record
    """
    storage = bd.get_empty_storage()
    base_level = bd.get_base_level(record.prediction)
    storage['base_level'] = base_level
    storage['base_dir'] = ('L', 'R')[base_level]

    bd.get_embedding_levels(record.prediction, storage)
    bd.explicit_embed_and_overrides(storage)
    bd.resolve_weak_types(storage)
    bd.resolve_neutral_types(storage, False)
    bd.resolve_implicit_levels(storage, False)
    for i, j in enumerate(record):
        storage['chars'][i]['record'] = j
    bd.reorder_resolved_levels(storage, False)
    bd.apply_mirroring(storage, False)
    prediction = ''
    cuts = []
    confidences = []
    for ch in storage['chars']:
        # code point may have been mirrored
        prediction = prediction + ch['ch']
        cuts.append(ch['record'][1])
        confidences.append(ch['record'][2])
    return ocr_record(prediction, cuts, confidences)


def extract_polygons(im: Image.Image, bounds: Dict[str, Any]) -> Image:
    """
    Yields the subimages of image im defined in the list of bounding polygons
    with baselines preserving order.

    Args:
        im (PIL.Image.Image): Input image
        bounds (list): A list of tuples (x1, y1, x2, y2)

    Yields:
        (PIL.Image) the extracted subimage
    """
    if 'type' in bounds and bounds['type'] == 'baselines':
        old_settings = np.seterr(all='ignore')


        siz = im.size
        white = Image.new(im.mode, siz)
        for line in bounds['lines']:
            mask = Image.new('1', siz, 0)
            draw = ImageDraw.Draw(mask)
            draw.polygon([tuple(x) for x in line['boundary']], outline=1, fill=1)
            masked_line = Image.composite(im, white, mask)
            bl = np.array(line['baseline'])
            ls = np.dstack((bl[:-1:], bl[1::]))
            bisect_points = np.mean(ls, 2)
            norm_vec = (ls[...,1] - ls[...,0])[:,::-1]
            norm_vec_len = np.sqrt(np.sum(norm_vec**2, axis=1))
            unit_vec = norm_vec / np.tile(norm_vec_len, (2, 1)).T # without
                                                                  # multiplication
                                                                  # with (1,-1)-upper/
                                                                  # (-1, 1)-lower
            bounds = np.array(line['boundary'])
            src_points = np.stack([_test_intersect(bp, uv, bounds) for bp, uv in zip(bisect_points, unit_vec)])
            upper_dist = np.diag(distance_matrix(src_points[:,:2], bisect_points))
            upper_dist = np.dstack((np.zeros_like(upper_dist), upper_dist)).squeeze(0)
            lower_dist = np.diag(distance_matrix(src_points[:,2:], bisect_points))
            lower_dist = np.dstack((np.zeros_like(lower_dist), lower_dist)).squeeze(0)
            # map baseline points to straight baseline
            bl_dists = np.cumsum(np.diag(np.roll(squareform(pdist(bl)), 1)))
            bl_dst_pts = bl[0] + np.dstack((bl_dists, np.zeros_like(bl_dists))).squeeze(0)
            rect_bisect_pts = np.mean(np.dstack((bl_dst_pts[:-1:], bl_dst_pts[1::])), 2)
            upper_dst_pts = rect_bisect_pts - upper_dist
            lower_dst_pts = rect_bisect_pts + lower_dist
            src_points = np.concatenate((bl, src_points[:,:2], src_points[:,2:]))
            dst_points = np.concatenate((bl_dst_pts, upper_dst_pts, lower_dst_pts))
            tform = PiecewiseAffineTransform()
            tform.estimate(src_points, dst_points)
            i = Image.fromarray((warp(masked_line, tform) * 255).astype('uint8'))
            yield i.crop(i.getbbox()), line
    else:
        if bounds['text_direction'].startswith('vertical'):
            angle = 90
        else:
            angle = 0
        for box in bounds['boxes']:
            if isinstance(box, tuple):
                box = list(box)
            if (box < [0, 0, 0, 0] or box[::2] > [im.size[0], im.size[0]] or
                    box[1::2] > [im.size[1], im.size[1]]):
                logger.error('bbox {} is outside of image bounds {}'.format(box, im.size))
                raise KrakenInputException('Line outside of image bounds')
            yield im.crop(box).rotate(angle, expand=True), box

def _test_intersect(bp, uv, bs):
    """
    Returns the intersection points of a ray with direction `uv` from
    `bp` with a polygon `bs`.
    """
    u = bp - np.roll(bs, 2)
    v = bs - np.roll(bs, 2)
    points = []
    for dir in ((1,-1), (-1,1)):
        w = (uv * dir * (1,-1))[::-1]
        z = np.dot(v, w)
        t1 = np.cross(v, u) / z
        t2 = np.dot(u, w) / z
        t1 = t1[np.logical_and(t2 >= 0.0, t2 <= 1.0)]
        points.extend(bp + (t1[np.where(t1 >= 0)[0].min()] * (uv * dir)))
    return np.array(points)

def _compute_polygon_section(baseline, boundary, dist1, dist2):
    """
    Given a baseline, polygonal boundary, and two points on the baseline return
    the rectangle formed by the orthogonal cuts on that baseline segment.
    """
    # find baseline segments the points are in
    bl = np.array(baseline)
    dists = np.cumsum(np.diag(np.roll(squareform(pdist(bl)), 1)))
    segs_idx = np.searchsorted(dists, [dist1, dist2])
    segs = np.dstack((bl[segs_idx-1], bl[segs_idx+1]))
    # compute unit vector of segments (NOT orthogonal)
    norm_vec = (segs[...,1] - segs[...,0])
    norm_vec_len = np.sqrt(np.sum(norm_vec**2, axis=1))
    unit_vec = norm_vec / np.tile(norm_vec_len, (2, 1)).T
    # find point start/end point on segments
    seg_dists = [dist1, dist2] - dists[segs_idx-1]
    seg_points = segs[...,0] + (seg_dists * unit_vec.T).T
    # get intersects
    bounds = np.array(boundary)
    points = [_test_intersect(point, uv[::-1], bounds).round() for point, uv in zip(seg_points, unit_vec)]
    return points[0].tolist() + np.roll(points[1], 2).tolist()

def mm_rpred(nets: Dict[str, TorchSeqRecognizer],
             im: Image.Image,
             bounds: dict,
             pad: int = 16,
             bidi_reordering: bool = True,
             script_ignore: Optional[List[str]] = None) -> Generator[ocr_record, None, None]:
    """
    Multi-model version of kraken.rpred.rpred.

    Takes a dictionary of ISO15924 script identifiers->models and an
    script-annotated segmentation to dynamically select appropriate models for
    these lines.

    Args:
        nets (dict): A dict mapping ISO15924 identifiers to TorchSegRecognizer
                     objects. Recommended to be an defaultdict.
        im (PIL.Image.Image): Image to extract text from
        bounds (dict): A dictionary containing a 'boxes' entry
                        with a list of lists of coordinates (script, (x0, y0,
                        x1, y1)) of a text line in the image and an entry
                        'text_direction' containing
                        'horizontal-lr/rl/vertical-lr/rl'.
        pad (int): Extra blank padding to the left and right of text line
        bidi_reordering (bool): Reorder classes in the ocr_record according to
                                the Unicode bidirectional algorithm for correct
                                display.
        script_ignore (list): List of scripts to ignore during recognition
    Yields:
        An ocr_record containing the recognized text, absolute character
        positions, and confidence values for each character.

    Raises:
        KrakenInputException if the mapping between segmentation scripts and
        networks is incomplete.
    """
    im_str = get_im_str(im)
    logger.info('Running {} multi-script recognizers on {} with {} lines'.format(len(nets), im_str, len(bounds['boxes'])))

    miss = [x[0] for x in bounds['boxes'] if not nets.get(x[0])]
    if miss:
        raise KrakenInputException('Missing models for scripts {}'.format(miss))

    # build dictionary for line preprocessing
    ts = {}
    for script, network in nets.items():
        logger.debug('Loading line transforms for {}'.format(script))
        batch, channels, height, width = network.nn.input
        ts[script] = generate_input_transforms(batch, height, width, channels, pad)

    for line in bounds['boxes']:
        rec = ocr_record('', [], [])
        for script, (box, coords) in zip(map(lambda x: x[0], line),
                                         extract_boxes(im, {'text_direction': bounds['text_direction'],
                                                            'boxes': map(lambda x: x[1], line)})):
            # skip if script is set to ignore
            if script_ignore is not None and script in script_ignore:
                logger.info('Ignoring {} line segment.'.format(script))
                continue
            # check if boxes are non-zero in any dimension
            if sum(coords[::2]) == 0 or coords[3] - coords[1] == 0:
                logger.warning('Run with zero dimension. Skipping.')
                continue
            # try conversion into tensor
            try:
                logger.debug('Preparing run.')
                line = ts[script](box)
            except Exception:
                logger.warning('Conversion of line {} failed. Skipping.'.format(coords))
                yield ocr_record('', [], [])
                continue

            # check if line is non-zero
            if line.max() == line.min():
                logger.warning('Empty run. Skipping.')
                yield ocr_record('', [], [])
                continue

            logger.debug('Forward pass with model {}'.format(script))
            preds = nets[script].predict(line)

            # calculate recognized LSTM locations of characters
            logger.debug('Convert to absolute coordinates')
            scale = box.size[0]/(len(nets[script].outputs)-2 * pad)
            pred = ''.join(x[0] for x in preds)
            pos = []
            conf = []

            for _, start, end, c in preds:
                if bounds['text_direction'].startswith('horizontal'):
                    xmin = coords[0] + int(max((start-pad)*scale, 0))
                    xmax = coords[0] + max(int(min((end-pad)*scale, coords[2]-coords[0])), 1)
                    pos.append((xmin, coords[1], xmax, coords[3]))
                else:
                    ymin = coords[1] + int(max((start-pad)*scale, 0))
                    ymax = coords[1] + max(int(min((end-pad)*scale, coords[3]-coords[1])), 1)
                    pos.append((coords[0], ymin, coords[2], ymax))
                conf.append(c)
            rec.prediction += pred
            rec.cuts.extend(pos)
            rec.confidences.extend(conf)
        if bidi_reordering:
            logger.debug('BiDi reordering record.')
            yield bidi_record(rec)
        else:
            logger.debug('Emitting raw record')
            yield rec


def rpred(network: TorchSeqRecognizer,
          im: Image.Image,
          bounds: dict,
          pad: int = 16,
          bidi_reordering: bool = True) -> Generator[ocr_record, None, None]:
    """
    Uses a RNN to recognize text

    Args:
        network (kraken.lib.models.TorchSeqRecognizer): A TorchSegRecognizer
                                                        object
        im (PIL.Image.Image): Image to extract text from
        bounds (dict): A dictionary containing a 'boxes' entry with a list of
                       coordinates (x0, y0, x1, y1) of a text line in the image
                       and an entry 'text_direction' containing
                       'horizontal-lr/rl/vertical-lr/rl'.
        pad (int): Extra blank padding to the left and right of text line.
                   Auto-disabled when expected network inputs are incompatible
                   with padding.
        bidi_reordering (bool): Reorder classes in the ocr_record according to
                                the Unicode bidirectional algorithm for correct
                                display.
    Yields:
        An ocr_record containing the recognized text, absolute character
        positions, and confidence values for each character.
    """
    im_str = get_im_str(im)
    logger.info('Running recognizer on {} with {} lines'.format(im_str, len(bounds['boxes'])))
    logger.debug('Loading line transform')
    batch, channels, height, width = network.nn.input
    valid_norm = True
    if 'type' in bounds and bounds['type'] == 'baseline':
        valid_norm = False
    ts = generate_input_transforms(batch, height, width, channels, pad, valid_norm)

    for box, coords in extract_boxes(im, bounds):
        # check if boxes are non-zero in any dimension
        if 0 in box.size:
            logger.warning('bbox {} with zero dimension. Emitting empty record.'.format(coords))
            yield ocr_record('', [], [])
            continue
        # try conversion into tensor
        try:
            line = ts(box)
        except Exception:
            yield ocr_record('', [], [])
            continue
        # check if line is non-zero
        if line.max() == line.min():
            yield ocr_record('', [], [])
            continue

        preds = network.predict(line)
        # calculate recognized LSTM locations of characters
        # scale between network output and network input
        net_scale = line.shape[2]/network.outputs.shape[1]
        # scale between network input and original line
        in_scale = box.size[0]/(line.shape[2]-2*pad)

        def _scale_val(val, min_val, max_val):
            return int(round(min(max(((val*net_scale)-pad)*in_scale, min_val), max_val)))

        # XXX: fix bounding box calculation ocr_record for multi-codepoint labels.
        pred = ''.join(x[0] for x in preds)
        pos = []
        conf = []
        for _, start, end, c in preds:
            if 'type' in bounds and bounds['type'] == 'baseline':
                pos.append(_compute_polygon_section(coords['baseline'],
                                                    coords['boundary'],
                                                    _scale_val(start, 0, box.size[0]),
                                                    _scale_val(end, 0, box.size[0])))
            elif bounds['text_direction'].startswith('horizontal'):
                xmin = coords[0] + _scale_val(start, 0, box.size[0])
                xmax = coords[0] + _scale_val(end, 0, box.size[0])
                pos.append((xmin, coords[1], xmin, coords[3], xmax, coords[3], xmax, coords[1]))
            else:
                ymin = coords[1] + _scale_val(start, 0, box.size[1])
                ymax = coords[1] + _scale_val(start, 0, box.size[1])
                pos.append((coords[0], ymin, coords[2], ymin, coords[2], ymax, coords[0], ymax))
            conf.append(c)
        if bidi_reordering:
            logger.debug('BiDi reordering record.')
            yield bidi_record(ocr_record(pred, pos, conf))
        else:
            logger.debug('Emitting raw record')
            yield ocr_record(pred, pos, conf)
