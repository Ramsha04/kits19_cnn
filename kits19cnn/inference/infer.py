from kits19cnn.io.preprocess import Preprocessor
import numpy as np
import nibabel as nib
from os.path import join
from scipy.ndimage.filters import gaussian_filter
from batchgenerators.augmentations.utils import pad_nd_image
from kits19cnn.models.metrics import evaluate_official

class Predictor(Preprocessor):
    """
    Prediction with Test-Time Data Augmentation & Post-Processing
    """
    def __init__(self, model, weights_path, in_dir, out_dir, clip_values=None, cases=None, do_mirroring=True,
                 use_gaussian=False, n_repeats=1, regions_class_order=None):
        """
        Attributes:
            model: channels_first
            weights_path (str): Path to the .h5 file with the trained weights
            in_dir (str): directory with the input data. Should be the kits19/data directory.
            out_dir (str): output directory where you want to save each case
            clip_values (list, tuple): values you want to clip CT scans to
                * For whole dset, the [0.5, 99.5] percentiles are [-75.75658734213053, 349.4891265535317]
            cases: list of case folders to preprocess
            do_mirroring (boolean): whether or not you want to predict on all possible mirrored results
            use_gaussian (boolean): test-time gaussian noise
            n_repeats (int): Number of times to repeat the prediction process
            regions_class_order (tuple or None): classes you want the corresponding predicted labels to contain
        """
        super().__init__(in_dir=in_dir, out_dir=out_dir, clip_values=clip_values, cases=cases)
        self.model = model
        self.model.load_weights(weights_path)
        self.do_mirroring = do_mirroring
        self.use_gaussian = use_gaussian
        self.patch_size = model.inputs[0].shape.as_list()[-2:]
        self.n_classes = model.outputs[0].shape.as_list()[1]
        self.n_repeats = n_repeats
        self.regions_class_order = regions_class_order

    def predict(self, evaluate=True):
        """
        Main prediction function. Saves predictions, images, labels
        """
        # Generating data and saving them recursively
        tk_dices = []
        tu_dices = []
        for case in self.cases:
            print("Processing: {0}".format(case))
            image = nib.load(join(self.in_dir, case, "imaging.nii.gz")).get_fdata()
            label = nib.load(join(self.in_dir, case, "segmentation.nii.gz")).get_fdata()
            orig_shape = image.shape
            # preprocessing
            preprocessed_img, preprocessed_label, coords = self.preprocess_2d(image, label, coords=True)
            preprocessed_img = np.expand_dims(preprocessed_img, 0)
            preprocessed_label = np.expand_dims(preprocessed_label, 0)
            self.save_imgs(preprocessed_img, preprocessed_label, case)
            # predicting + post-processing
            pred, act_pred = self.predict_3D_2Dconv_tiled(preprocessed_img)
            pred = pad_nonint_extraction(pred, orig_shape, coords, pad_border_mode="constant")
            self.save_imgs(pred, mask=None, case=case, pred=True)
            if evaluate:
                tk_dice, tu_dice = evaluate_official(label, pred)
                print("Tumour and Kidney Dice: {0}; Tumour Dice: {1}".format(tk_dice, tu_dice))
                tk_dices.append(tk_dice), tu_dices.append(tu_dice)
        if evaluate:
            print("Average Tumour Kidney Dice: {0}\n".format(np.mean(tk_dices)) +
                  "Average Tumour Dice: {0}".format(np.mean(tu_dices)))

    def predict_3D_2Dconv_tiled(self, data, BATCH_SIZE=None, mirror_axes=(0, 1),
                                step=2, pad_border_mode="edge", pad_kwargs=None):
        """
        Args:
            data (numpy array): shape of (c, x, y, z)
            BATCH_SIZE (int): Batch size
            mirror_axes (list, tuple): for each spatial dimension (0,1)
            steps (int):
            pad_border_mode (str):
            pad_kwargs:
        """
        assert len(data.shape) == 4, "data must be c, x, y, z"
        predicted_segmentation = []
        act_pred = []
        for s in range(data.shape[1]):
            pred_seg, act_pres = \
                self.predict_2D_2Dconv_tiled(data[:, s], BATCH_SIZE, step,
                                             mirror_axes, pad_border_mode=pad_border_mode,
                                             pad_kwargs=pad_kwargs)
            predicted_segmentation.append(pred_seg[None])
            act_pred.append(act_pres[None])
        predicted_segmentation = np.vstack(predicted_segmentation)
        act_pred = np.vstack(act_pred).transpose((1, 0, 2, 3))
        return predicted_segmentation, act_pred

    def predict_2D_2Dconv_tiled(self, patient_data, BATCH_SIZE=None, step=2,
                                mirror_axes=(0, 1), pad_border_mode="edge", pad_kwargs=None):
        """
        Args:
            patient_data: shape of (c, x, y)
            BATCH_SIZE (int): Batch size
            mirror_axes (list, tuple): for each spatial dimension (0,1)
            steps (int):
            pad_border_mode (str):
            pad_kwargs:
        """
        tile_size = self.patch_size
        assert tile_size is not None, "patch_size cannot be None for tiled prediction"
        # pad images so that their size is a multiple of tile_size
        data, slicer = pad_nd_image(patient_data, tile_size, pad_border_mode, pad_kwargs, True)
        # adds the channels dimension
        data = data[None]

        if BATCH_SIZE is not None:
            data = np.vstack([data] * BATCH_SIZE)

        input_size = [1, patient_data.shape[0]] + list(tile_size)
        input_size = [int(i) for i in input_size] # rounding

        result = np.zeros([self.n_classes] + list(data.shape[2:]), dtype=np.float32)
        result_numsamples = np.zeros([self.n_classes] + list(data.shape[2:]), dtype=np.float32)
        if self.use_gaussian:
            tmp = np.zeros(tile_size, dtype=np.float32)
            center_coords = [i//2 for i in tile_size]
            sigmas = [i // 8 for i in tile_size]
            tmp[tuple(center_coords)] = 1
            tmp_smooth = gaussian_filter(tmp, sigmas, 0, mode='constant', cval=0)
            tmp_smooth = tmp_smooth / tmp_smooth.max() * 1
            add = tmp_smooth
        else:
            add = np.ones(tile_size)

        data_shape = data.shape
        center_coord_start = np.array([dim//2 for dim in self.patch_size]).astype(int) #lb center of patch
        # i+2 because of batch size and channels dimension
        # ub center coords
        center_coord_end = np.array([data_shape[i + 2] - self.patch_size[i] // 2 for i in range(len(self.patch_size))]).astype(int)
        # number of total steps to extract from based on the specified step size
        n_steps = np.ceil([(center_coord_end[i] - center_coord_start[i]) / (self.patch_size[i] / step) for i in range(2)])
        # how big each step based on the number steps and the coords
        # Why use the center coordinates? b/c better results? cleaner crop
        step_size = np.array([(center_coord_end[i] - center_coord_start[i]) / (n_steps[i] + 1e-8) for i in range(2)])
        step_size[step_size == 0] = 9999999 # what does this deal with? when n_steps = 0, so when the patch size is close to
                                            # the data shape
        # center patch coords to extract from
        xsteps = np.round(np.arange(center_coord_start[0], center_coord_end[0]+1e-8, step_size[0])).astype(int)
        ysteps = np.round(np.arange(center_coord_start[1], center_coord_end[1]+1e-8, step_size[1])).astype(int)
        # center cropping at each coordinate
        for x in xsteps:
            lb_x = x - self.patch_size[0] // 2
            ub_x = x + self.patch_size[0] // 2
            for y in ysteps:
                lb_y = y - self.patch_size[1] // 2
                ub_y = y + self.patch_size[1] // 2
                result[:, lb_x:ub_x, lb_y:ub_y] += \
                    self.pred_per_2D(data[:, :, lb_x:ub_x, lb_y:ub_y], mirror_axes, add).squeeze()
                # important for averaging
                result_numsamples[:, lb_x:ub_x, lb_y:ub_y] += add
        # Removing the padding that was added in the beginning by pad_nd_image
        slicer = tuple([slice(0, result.shape[i]) for i in range(len(result.shape) - (len(slicer) - 1))] + slicer[1:])
        result = result[slicer]
        result_numsamples = result_numsamples[slicer]
        # completing the averaging
        act_pred = result / result_numsamples

        if self.regions_class_order is None:
            predicted_segmentation = act_pred.argmax(0)
        else:
            predicted_segmentation_shp = act_pred[0].shape
            predicted_segmentation = np.zeros(predicted_segmentation_shp, dtype=np.float32)
            for i, c in enumerate(self.regions_class_order):
                predicted_segmentation[act_pred[i] > 0.5] = c
        return predicted_segmentation, act_pred

    def pred_per_2D(self, x, mirror_axes, mult=None):
        """
        Args:
            x: numpy array
            mirror_axes (list, tuple): for each spatial dimension (0,1)
            mult (boolean): factor to multiply results by
        """
        result = np.zeros([1, self.n_classes] + list(x.shape[2:]))
        n_results = self.n_repeats
        if self.do_mirroring:
            mirror_idx = 4
            n_results *= 2 ** len(mirror_axes)
        else:
            mirror_idx = 1

        for i in range(self.n_repeats):
            for m in range(mirror_idx):
                if m == 0:
                    pred = self.model.predict(x)
                    result += 1/n_results * pred

                if m == 1 and (1 in mirror_axes):
                    pred = self.model.predict(np.flip(x, 3))
                    result += 1/n_results * np.flip(pred, 3)

                if m == 2 and (0 in mirror_axes):
                    pred = self.model.predict(np.flip(x, 2))
                    result += 1/n_results * np.flip(pred, 2)

                if m == 3 and (0 in mirror_axes) and (1 in mirror_axes):
                    pred = self.model.predict(np.flip(np.flip(x, 3), 2))
                    result += 1/n_results * np.flip(np.flip(pred, 3), 2)

        if mult is not None:
            result[:, :] *= mult

        return result

def pad_nonint_extraction(image, orig_shape, coords, pad_border_mode="edge", pad_kwargs={}):
    """
    Pads the cropped output from the extract_nonint_region function
    Args:
        image: either the mask or the thresholded (= 0.5) segmentation prediction (x, y, z)
        orig_shape: Original shape of the 3D volume (no channels)
        coords: outputted coordinates from `extract_nonint_region`
    Returns:
        padded: numpy array of shape `orig_shape`
    """
    # trying to reverse the cropping with padding
    padding = [[coords[i][0], orig_shape[i]-coords[i][1]] for i in range(len(orig_shape))]
    padded = np.pad(image, padding, mode=pad_border_mode, **pad_kwargs)
    return padded
