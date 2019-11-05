import os
import numpy as np
import nibabel as nib

from torch.utils.data import Dataset

class VoxelDataset(Dataset):
    def __init__(self, im_ids: np.array,
                 transforms=None,
                 preprocessing=None,
                 file_ending=".npy"):
        """
        Attributes
            im_ids (np.ndarray): of image names.
            transforms (albumentations.augmentation): transforms to apply
                before preprocessing. Defaults to HFlip and ToTensor
            preprocessing: ops to perform after transforms, such as
                z-score standardization. Defaults to None.
            file_ending (str): one of ['.npy', '.nii', '.nii.gz']
        """
        self.im_ids = im_ids
        self.transforms = transforms
        self.preprocessing = preprocessing
        self.file_ending = file_ending
        print(f"Using the {file_ending} files...")

    def __getitem__(self, idx):
        # loads data as a numpy arr and then adds the channel + batch size dimensions
        case_id = self.im_ids[idx]
        x, y = self.load_volume(case_id)
        if self.transforms:
            data_dict = self.transforms(**{"data": x, "seg": y})
            x, y = data_dict["data"], data_dict["seg"]
        if self.preprocessing:
            preprocessed = self.preprocessing(**{"data": x, "seg": y})
            x, y = preprocessed["data"], preprocessed["seg"]
        return img, mask

    def __len__(self):
        return len(self.im_ids)

    def load_volume(self, case_id):
        """
        Loads volume from either .npy or nifti files.
        Args:
            case_id: path to the case folder
                i.e. /content/kits19/data/case_00001
        Returns:
            Tuple of:
            - x (np.ndarray): shape (1, d, h, w)
            - y (np.ndarray): same shape as x
        """
        x_path = os.path.join(case_id, f"imaging{self.file_ending}")
        y_path = os.path.join(case_id, f"segmentation{self.file_ending}")
        if self.file_ending == ".npy":
            x, y = np.load(x_path), np.load(y_path)
        elif self.file_ending == ".nii.gz" or self.file_ending == ".nii":
            x, y = nib.load(x_path).get_fdata(), nib.load(y_path).get_fdata()
        return (x[None], y[None])