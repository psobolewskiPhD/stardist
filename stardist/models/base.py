from __future__ import print_function, unicode_literals, absolute_import, division
from six.moves import range, zip, map, reduce, filter
from six import string_types

import numpy as np
import warnings
import math
from tqdm import tqdm

import keras.backend as K
from keras.utils import Sequence
from keras.optimizers import Adam
from keras.callbacks import ReduceLROnPlateau, TensorBoard
from csbdeep.models import BaseConfig, BaseModel
from csbdeep.utils.tf import CARETensorBoard
from csbdeep.utils import _raise, backend_channels_last, axes_check_and_normalize, axes_dict
from csbdeep.internals.predict import tile_iterator
from csbdeep.data import Resizer



# TODO: support (optional) classification of objects?
# TODO: helper function to check if receptive field of cnn is sufficient for object sizes in GT

def generic_masked_loss(mask, loss, weights=1, norm_by_mask=True, reg_weight=0, reg_penalty=K.abs):
    def _loss(y_true, y_pred):
        actual_loss = K.mean(mask * weights * loss(y_true, y_pred), axis=-1)
        norm_mask = (K.mean(mask) + K.epsilon()) if norm_by_mask else 1
        if reg_weight > 0:
            reg_loss = K.mean((1-mask) * reg_penalty(y_pred), axis=-1)
            return actual_loss / norm_mask + reg_weight * reg_loss
        else:
            return actual_loss / norm_mask
    return _loss

def masked_loss(mask, penalty, reg_weight, norm_by_mask):
    loss = lambda y_true, y_pred: penalty(y_true - y_pred)
    return generic_masked_loss(mask, loss, reg_weight=reg_weight, norm_by_mask=norm_by_mask)

# TODO: should we use norm_by_mask=True in the loss or only in a metric?
#       previous 2D behavior was norm_by_mask=False
#       same question for reg_weight? use 1e-4 (as in 3D) or 0 (as in 2D)?

def masked_loss_mae(mask, reg_weight=0, norm_by_mask=True):
    return masked_loss(mask, K.abs, reg_weight=reg_weight, norm_by_mask=norm_by_mask)

def masked_loss_mse(mask, reg_weight=0, norm_by_mask=True):
    return masked_loss(mask, K.square, reg_weight=reg_weight, norm_by_mask=norm_by_mask)

def masked_metric_mae(mask):
    def relevant_mae(y_true, y_pred):
        return masked_loss(mask, K.abs, reg_weight=0, norm_by_mask=True)(y_true, y_pred)
    return relevant_mae

def masked_metric_mse(mask):
    def relevant_mse(y_true, y_pred):
        return masked_loss(mask, K.square, reg_weight=0, norm_by_mask=True)(y_true, y_pred)
    return relevant_mse

def kld(y_true, y_pred):
    y_true = K.clip(y_true, K.epsilon(), 1)
    y_pred = K.clip(y_pred, K.epsilon(), 1)
    return K.mean(K.binary_crossentropy(y_true, y_pred) - K.binary_crossentropy(y_true, y_true), axis=-1)



class StarDistDataBase(Sequence):

    def __init__(self, X, Y, n_rays, grid, batch_size, patch_size, use_gpu=False, maxfilter_cache=True, maxfilter_patch_size=None, augmenter=None):

        self.X, self.Y = X, Y
        self.batch_size = batch_size
        self.n_rays = n_rays
        self.patch_size = patch_size
        self.ss_grid = (slice(None),) + tuple(slice(0, None, g) for g in grid)
        self.perm = np.random.permutation(len(self.X))
        self.use_gpu = bool(use_gpu)
        if augmenter is None:
            augmenter = lambda *args: args
        callable(augmenter) or _raise(ValueError("augmenter must be None or callable"))
        self.augmenter = augmenter

        if self.use_gpu:
            from gputools import max_filter
            self.max_filter = lambda y, patch_size: max_filter(y.astype(np.float32), patch_size)
        else:
            from scipy.ndimage.filters import maximum_filter
            self.max_filter = lambda y, patch_size: maximum_filter(y, patch_size, mode='constant')

        self.maxfilter_patch_size = maxfilter_patch_size if maxfilter_patch_size is not None else self.patch_size

        if maxfilter_cache:
            self.R = [self.no_background_patches((x,y)) for x,y in zip(self.X,self.Y)]
        else:
            self.R = None


    def __len__(self):
        return int(np.ceil(len(self.X) / float(self.batch_size)))


    def on_epoch_end(self):
        self.perm = np.random.permutation(len(self.X))


    def no_background_patches(self, arrays, *args):
        x, y = arrays
        return self.max_filter(y, self.maxfilter_patch_size) > 0


    def no_background_patches_cached(self, k):
        if self.R is None:
            return self.no_background_patches
        else:
            return lambda *args: self.R[k]



class StarDistBase(BaseModel):

    def prepare_for_training(self, optimizer=None):
        """Prepare for neural network training.

        Compiles the model and creates
        `Keras Callbacks <https://keras.io/callbacks/>`_ to be used for training.

        Note that this method will be implicitly called once by :func:`train`
        (with default arguments) if not done so explicitly beforehand.

        Parameters
        ----------
        optimizer : obj or None
            Instance of a `Keras Optimizer <https://keras.io/optimizers/>`_ to be used for training.
            If ``None`` (default), uses ``Adam`` with the learning rate specified in ``config``.

        """
        if optimizer is None:
            optimizer = Adam(lr=self.config.train_learning_rate)

        input_mask = self.keras_model.inputs[1] # second input layer is mask for dist loss
        dist_loss = {'mse': masked_loss_mse, 'mae': masked_loss_mae}[self.config.train_dist_loss](input_mask, reg_weight=self.config.train_background_reg)
        prob_loss = 'binary_crossentropy'
        self.keras_model.compile(optimizer, loss=[prob_loss, dist_loss],
                                            loss_weights = list(self.config.train_loss_weights),
                                            metrics={'prob': kld, 'dist': [masked_metric_mae(input_mask),masked_metric_mse(input_mask)]})

        self.callbacks = []
        if self.basedir is not None:
            self.callbacks += self._checkpoint_callbacks()

            if self.config.train_tensorboard:
                # self.callbacks.append(TensorBoard(log_dir=str(self.logdir), write_graph=False))
                self.callbacks.append(CARETensorBoard(log_dir=str(self.logdir), prefix_with_timestamp=False, n_images=3, write_images=True, prob_out=False))

        if self.config.train_reduce_lr is not None:
            rlrop_params = self.config.train_reduce_lr
            if 'verbose' not in rlrop_params:
                rlrop_params['verbose'] = True
            self.callbacks.append(ReduceLROnPlateau(**rlrop_params))

        self._model_prepared = True


    def predict(self, img, axes=None, normalizer=None, n_tiles=None, show_tile_progress=True, **predict_kwargs):
        """Predict.

        Parameters
        ----------
        img : :class:`numpy.ndarray`
            Input image
        axes : str or None
            Axes of the input ``img``.
            ``None`` denotes that axes of img are the same as defnoted in the config.
        normalizer : :class:`csbdeep.data.Normalizer` or None
            (Optional) normalization of input image before prediction.
        n_tiles : iterable or None
            Out of memory (OOM) errors can occur if the input image is too large.
            To avoid this problem, the input image is broken up into (overlapping) tiles
            that are processed independently and re-assembled.
            This parameter denotes a tuple of the number of tiles for every image axis (see ``axes``).
            ``None`` denotes that no tiling should be used.
        show_tile_progress: bool
            Whether to show progress during tiled prediction.
        predict_kwargs: dict
            Keyword arguments for ``predict`` function of Keras model.

        Returns
        -------
        (:class:`numpy.ndarray`,:class:`numpy.ndarray`)
            Returns the tuple (`prob`, `dist`) of per-pixel object probabilities and star-convex polygon distances.

        """
        if n_tiles is None:
            n_tiles = [1]*img.ndim
        try:
            n_tiles = tuple(n_tiles)
            img.ndim == len(n_tiles) or _raise(TypeError())
        except TypeError:
            raise ValueError("n_tiles must be an iterable of length %d" % img.ndim)
        all(np.isscalar(t) and 1<=t and int(t)==t for t in n_tiles) or _raise(
            ValueError("all values of n_tiles must be integer values >= 1"))
        n_tiles = tuple(map(int,n_tiles))

        if axes is None:
            axes = self.config.axes
            assert 'C' in axes
            if img.ndim == len(axes)-1 and self.config.n_channel_in == 1:
                # img has no dedicated channel axis, but 'C' always part of config axes
                axes = axes.replace('C','')

        axes     = axes_check_and_normalize(axes,img.ndim)
        axes_net = self.config.axes

        _permute_axes = self._make_permute_axes(axes, axes_net)
        x = _permute_axes(img) # x has axes_net semantics

        channel = axes_dict(axes_net)['C']
        self.config.n_channel_in == x.shape[channel] or _raise(ValueError())
        axes_net_div_by = self._axes_div_by(axes_net)

        grid = tuple(self.config.grid)
        len(grid) == len(axes_net)-1 or _raise(ValueError())
        grid_dict = dict(zip(axes_net.replace('C',''),grid))

        normalizer = self._check_normalizer_resizer(normalizer, None)[0]
        resizer = StarDistPadAndCropResizer(grid=grid_dict)

        x = normalizer.before(x, axes_net)
        x = resizer.before(x, axes_net, axes_net_div_by)

        def predict_direct(tile):
            sh = list(tile.shape); sh[channel] = 1; dummy = np.empty(sh,np.float32)
            prob, dist = self.keras_model.predict([tile[np.newaxis],dummy[np.newaxis]], **predict_kwargs)
            return prob[0], dist[0]

        if np.prod(n_tiles) > 1:
            tiling_axes   = axes_net.replace('C','') # axes eligible for tiling
            x_tiling_axis = tuple(axes_dict(axes_net)[a] for a in tiling_axes) # numerical axis ids for x
            axes_net_tile_overlaps = self._axes_tile_overlap(axes_net)
            # hack: permute tiling axis in the same way as img -> x was permuted
            n_tiles = _permute_axes(np.empty(n_tiles,np.bool)).shape
            (all(n_tiles[i] == 1 for i in range(x.ndim) if i not in x_tiling_axis) or
                _raise(ValueError("entry of n_tiles > 1 only allowed for axes '%s'" % tiling_axes)))

            sh = [s//grid_dict.get(a,1) for a,s in zip(axes_net,x.shape)]
            sh[channel] = 1;                  prob = np.empty(sh,np.float32)
            sh[channel] = self.config.n_rays; dist = np.empty(sh,np.float32)

            n_block_overlaps = [int(np.ceil(overlap/blocksize)) for overlap, blocksize
                                in zip(axes_net_tile_overlaps, axes_net_div_by)]

            for tile, s_src, s_dst in tqdm(tile_iterator(x, n_tiles, block_sizes=axes_net_div_by, n_block_overlaps=n_block_overlaps),
                                           disable=(not show_tile_progress), total=np.prod(n_tiles)):
                prob_tile, dist_tile = predict_direct(tile)
                # account for grid
                s_src = [slice(s.start//grid_dict.get(a,1),s.stop//grid_dict.get(a,1)) for s,a in zip(s_src,axes_net)]
                s_dst = [slice(s.start//grid_dict.get(a,1),s.stop//grid_dict.get(a,1)) for s,a in zip(s_dst,axes_net)]
                # prob and dist have different channel dimensionality than image x
                s_src[channel] = slice(None)
                s_dst[channel] = slice(None)
                s_src, s_dst = tuple(s_src), tuple(s_dst)
                # print(s_src,s_dst)
                prob[s_dst] = prob_tile[s_src]
                dist[s_dst] = dist_tile[s_src]

        else:
            prob, dist = predict_direct(x)

        prob = resizer.after(prob, axes_net)
        dist = resizer.after(dist, axes_net)
        dist = np.maximum(1e-3, dist) # avoid small/negative dist values to prevent problems with Qhull

        prob = np.take(prob,0,axis=channel)
        dist = np.moveaxis(dist,channel,-1)

        return prob, dist


    def predict_instances(self, img, axes=None, normalizer=None, prob_thresh=0.5, nms_thresh=0.5,
                          return_polygons=False, n_tiles=None, show_tile_progress=True,
                          predict_kwargs=None, nms_kwargs=None):
        if predict_kwargs is None:
            predict_kwargs = {}
        if nms_kwargs is None:
            nms_kwargs = {}

        # TODO: always return label image, details of instances

        prob, dist = self.predict(img, axes=axes, normalizer=normalizer,
                                  n_tiles=n_tiles, show_tile_progress=show_tile_progress, **predict_kwargs)

        return self._instances_from_prediction(img.shape, prob, dist, prob_thresh=prob_thresh, nms_thresh=nms_thresh,
                                               return_polygons=return_polygons, **nms_kwargs)



class StarDistPadAndCropResizer(Resizer):

    # TODO: check correctness
    def __init__(self, grid, mode='reflect', **kwargs):
        assert isinstance(grid, dict)
        self.mode = mode
        self.grid = grid
        self.kwargs = kwargs


    def before(self, x, axes, axes_div_by):
        assert all(a%g==0 for g,a in zip((self.grid.get(a,1) for a in axes), axes_div_by))
        axes = axes_check_and_normalize(axes,x.ndim)
        def _split(v):
            return 0, v # only pad at the end
        self.pad = {
            a : _split((div_n-s%div_n)%div_n)
            for a, div_n, s in zip(axes, axes_div_by, x.shape)
        }
        x_pad = np.pad(x, tuple(self.pad[a] for a in axes), mode=self.mode, **self.kwargs)
        self.padded_shape = dict(zip(axes,x_pad.shape))
        if 'C' in self.padded_shape: del self.padded_shape['C']
        return x_pad


    def after(self, x, axes):
        # axes can include 'C', which may not have been present in before()
        axes = axes_check_and_normalize(axes,x.ndim)
        assert all(s_pad == s * g for s,s_pad,g in zip(x.shape,
                                                       (self.padded_shape.get(a,_s) for a,_s in zip(axes,x.shape)),
                                                       (self.grid.get(a,1) for a in axes)))
        # print(self.padded_shape)
        # print(self.pad)
        # print(self.grid)
        crop = tuple (
            slice(0, -(math.floor(p[1]/g)) if p[1]>=g else None)
            for p,g in zip((self.pad.get(a,(0,0)) for a in axes),(self.grid.get(a,1) for a in axes))
        )
        # print(crop)
        return x[crop]