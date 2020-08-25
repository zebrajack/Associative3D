"""
Multiview suncg dataset to train Siamese RelNet.
"""
import os
import os.path as osp
import numpy as np
import collections
import cv2
import scipy.misc
import scipy.linalg
import scipy.io as sio
import scipy.ndimage.interpolation
from absl import flags
import pickle as pkl
import torch
import multiprocessing
from multiprocessing import Manager
from torch.utils.data import Dataset
from torch.utils.data import DataLoader
from torch.utils.data import RandomSampler
from torch.utils.data.dataloader import default_collate
import pdb
from datetime import datetime
import sys
import imageio
import itertools

from ..utils import suncg_parse
from ..renderer import utils as render_utils

# backends
torch.multiprocessing.set_sharing_strategy('file_system')

#-------------- flags -------------#
#----------------------------------#
flags.DEFINE_string('suncg_dir', '/w/syqian/suncg', 'Suncg Data Directory')
flags.DEFINE_boolean('filter_objects', True, 'Restrict object classes to main semantic classes.')
flags.DEFINE_integer('max_views_per_house', 0, '0->use all views. Else we randomly select upto the specified number.')

flags.DEFINE_boolean('suncg_dl_out_codes', True, 'Should the data loader load codes')
flags.DEFINE_boolean('suncg_dl_out_paths', False, 'Should the data loader load  paths')
flags.DEFINE_boolean('suncg_dl_out_layout', False, 'Should the data loader load layout')
flags.DEFINE_boolean('suncg_dl_out_depth', False, 'Should the data loader load modal depth')
flags.DEFINE_boolean('suncg_dl_out_fine_img', True, 'We should output fine images')
flags.DEFINE_boolean('suncg_dl_out_voxels', False, 'We should output scene voxels')
flags.DEFINE_boolean('suncg_dl_out_proposals', False, 'We should edgebox proposals for training')
flags.DEFINE_boolean('suncg_dl_out_only_pos_proposals', True, 'We should only output +ve edgebox proposals for training')
flags.DEFINE_boolean('suncg_dl_out_test_proposals', False, 'We should edgebox proposals for testing')
flags.DEFINE_integer('suncg_dl_max_proposals', 40, 'Max number of proposals per image')

flags.DEFINE_integer('img_height', 128, 'image height')
flags.DEFINE_integer('img_width', 256, 'image width')

flags.DEFINE_integer('img_height_fine', 480, 'image height')
flags.DEFINE_integer('img_width_fine', 640, 'image width')

flags.DEFINE_integer('layout_height', 64, 'amodal depth height : should be half image height')
flags.DEFINE_integer('layout_width', 128, 'amodal depth width : should be half image width')

flags.DEFINE_integer('max_object_classes', 10, 'maximum object classes')

flags.DEFINE_integer('voxels_height', 32, 'scene voxels height. Should be half of width and depth.')
flags.DEFINE_integer('voxels_width', 64, 'scene voxels width')
flags.DEFINE_integer('voxels_depth', 64, 'scene voxels depth')
flags.DEFINE_boolean('suncg_dl_debug_mode', False, 'Just running for debugging, should not preload ojects')

flags.DEFINE_boolean('use_trans_scale', False, 'scale trans to 0-1')
flags.DEFINE_boolean('relative_trj', True, 'use relative trjs')

flags.DEFINE_string('split_file', 'train.txt', 'training/validation/test split file')

#------------- Dataset ------------#
#----------------------------------#
class SuncgDataset(Dataset):
    '''SUNCG data loader'''
    def __init__(self, house_names, opts):
        self._suncg_dir = opts.suncg_dir
        self._house_names = house_names
        self.img_size = (opts.img_height, opts.img_width)
        self.output_fine_img = opts.suncg_dl_out_fine_img
        if self.output_fine_img:
            self.img_size_fine = (opts.img_height_fine, opts.img_width_fine)
        self.output_codes = opts.suncg_dl_out_codes
        self.output_layout = opts.suncg_dl_out_layout
        self.output_modal_depth = opts.suncg_dl_out_depth
        self.output_voxels = opts.suncg_dl_out_voxels
        self.output_proposals = opts.suncg_dl_out_proposals
        self.output_test_proposals = opts.suncg_dl_out_test_proposals
        self.output_paths = opts.suncg_dl_out_paths
        self.relative_trj = opts.relative_trj
        self.use_trans_scale = opts.use_trans_scale
        self.max_object_classes = opts.max_object_classes
        self.only_pos_proposals = opts.suncg_dl_out_only_pos_proposals
        self.Adict = {}
        self.lmbda = 1
        if self.output_layout or self.output_modal_depth:
            self.layout_size = (opts.layout_height, opts.layout_width)
        if self.output_voxels:
            self.voxels_size = (opts.voxels_width, opts.voxels_height, opts.voxels_depth)

        if self.output_proposals:
            self.max_proposals = opts.suncg_dl_max_proposals
        if self.output_codes:
            self.max_rois = opts.max_rois
            self._obj_loader = suncg_parse.ObjectLoader(osp.join(opts.suncg_dir, 'object'))
            if not opts.suncg_dl_debug_mode:
                self._obj_loader.preload()
            if opts.filter_objects:
                self._meta_loader = suncg_parse.MetaLoader(osp.join(opts.suncg_dir, 'ModelCategoryMappingEdited.csv'))
            else:
                self._meta_loader = None

        print("loading split files...")
        self._data_tuples = []
        self.parse_split_file(opts.split_file)
        self.n_imgs = len(self._data_tuples)

        self._preload_cameras(house_names)
        print('Using object classes {}'.format(suncg_parse.valid_object_classes))

    def parse_split_file(self, split_file):
        data_tuples = []
        # read data split
        f = open(split_file, 'r')
        lines = f.readlines()
        f.close()
        lines = lines[3:]
        #lines = lines[:10]
        for line in lines:
            # parse line
            splits = line.split(' ')
            img1_path = splits[0]
            img2_path = splits[8]
            house_id = img1_path.split('/')[-2]
            img1_name = img1_path.split('/')[-1]
            img2_name = img2_path.split('/')[-1]
            view1 = img1_name[:6]
            view2 = img2_name[:6]
            data_tuples.append((house_id, view1, view2))
            
        self._data_tuples = data_tuples

    def forward_img(self, house, view_id):
        #house, view_id = self._data_tuples[index]
        try:
            img = imageio.imread(osp.join(self._suncg_dir, 'renderings_ldr', house, view_id + '_mlt.jpg'))
        except:
            img = imageio.imread(osp.join(self._suncg_dir, 'renderings_ldr', house, view_id + '_mlt.png'))
        if len(img.shape) == 2:
            ## Image is corrupted and it does not have third channel.
            ### Repeat the sample image 3 times.
            #house, view_id = self._data_tuples[index]
            print("Corrupted Image Type 1 {} , {}".format(house, view_id))
            img = np.repeat(np.expand_dims(img,2),3, axis=2)

        if img.shape[2] == 2:
            #house, view_id = self._data_tuples[index]
            print("Corrupted Image Type 2 {} , {}".format(house, view_id))
            img = np.concatenate((img, img[:, :, 0:1]), axis=2)

        if self.output_fine_img:
            img_fine = cv2.resize(img, (self.img_size_fine[1], self.img_size_fine[0]))
            img_fine = np.transpose(img_fine, (2,0,1))

        img = cv2.resize(img, (self.img_size[1], self.img_size[0]))
        img = np.transpose(img, (2,0,1))
        if self.output_fine_img:
            return img/255, img_fine/255, house, view_id
        else:
            return img/255, house, view_id

    def _preload_cameras(self, house_names):
        self._house_cameras = {}
        for hx, house in enumerate(house_names):
            if (hx % 200) == 0:
                print('Pre-loading cameras from house {}/{}'.format(hx, len(house_names)))
            cam_file = osp.join(self._suncg_dir, 'camera', house, 'room_camera.txt')
            camera_poses = suncg_parse.read_camera_pose(cam_file)
            self._house_cameras[house] = camera_poses

    def forward_codes(self, house_name, view_id):
        campose = self._house_cameras[house_name][int(view_id)]
        cam2world = suncg_parse.campose_to_extrinsic(campose).astype(np.float32)
        world2cam = scipy.linalg.inv(cam2world).astype(np.float32)

        house_data = suncg_parse.load_json(
            osp.join(self._suncg_dir, 'house', house_name, 'house.json'))
        bbox_data = sio.loadmat(
            osp.join(self._suncg_dir, 'bboxes_node', house_name, view_id + '_bboxes.mat'))

        objects_data, objects_bboxes, select_node_ids, _ = suncg_parse.select_ids_multi(
            house_data, bbox_data, meta_loader=self._meta_loader, min_pixels=500)
        
        objects_codes, _ = suncg_parse.codify_room_data(
            objects_data, world2cam, self._obj_loader,
            max_object_classes = self.max_object_classes)

        objects_bboxes -= 1 #0 indexing to 1 indexing
        if len(objects_codes) > self.max_rois:
            select_inds = np.random.permutation(len(objects_codes))[0:self.max_rois]
            objects_bboxes = objects_bboxes[select_inds, :].copy()
            objects_codes = [objects_codes[ix] for ix in select_inds]
            select_node_ids = [select_node_ids[ix] for ix in select_inds]
            
        # return objects_codes, objects_bboxes, extra_codes
        return objects_codes, objects_bboxes, select_node_ids

    def forward_proposals(self, house_name, view_id, codes_gt, bboxes_gt):
        proposals_data = sio.loadmat(
            osp.join(self._suncg_dir, 'edgebox_proposals', house_name, view_id + '_proposals.mat'))
        bboxes_proposals = proposals_data['proposals'][:,0:4]
        bboxes_proposals -= 1 #zero indexed
        codes, bboxes, labels = suncg_parse.extract_proposal_codes(
            codes_gt, bboxes_gt, bboxes_proposals, self.max_proposals,
            only_pos_proposals = self.only_pos_proposals)
        return codes, bboxes, labels
    
    def forward_test_proposals(self, house_name, view_id):
        proposals_data = sio.loadmat(
            osp.join(self._suncg_dir, 'edgebox_proposals', house_name, view_id + '_proposals.mat'))
        bboxes_proposals = proposals_data['proposals'][:,0:4]
        bboxes_proposals -= 1 #zero indexed
        return bboxes_proposals

    def forward_layout(self, house_name, view_id, bg_depth=1e4):
        depth_im = imageio.imread(osp.join(
            self._suncg_dir, 'renderings_layout', house_name, view_id + '_depth.png'))
        depth_im =  depth_im.astype(np.float)/1000.0  # depth was saved in mm
        depth_im += bg_depth*np.equal(depth_im,0).astype(np.float)
        disp_im = 1./depth_im
        amodal_depth = scipy.ndimage.interpolation.zoom(
            disp_im, (self.layout_size[0]/disp_im.shape[0], self.layout_size[1]/disp_im.shape[1]), order=0)
        amodal_depth = np.reshape(amodal_depth, (1, self.layout_size[0], self.layout_size[1]))
        return amodal_depth

    def forward_max_depth(self, house_name, view_id, bg_depth=1e4):
        depth_im = imageio.imread(osp.join(
            self._suncg_dir, 'renderings_layout', house_name,
            view_id + '_depth.png'))
        depth_im = depth_im.astype(np.float) / 1000.0  # depth was saved in mm
        max_depth = np.max(depth_im)
        if max_depth < 1E-3:
            max_depth = np.max(depth_im) + 1000
        return max_depth
    
    def forward_voxels(self, house_name, view_id):
        scene_voxels = sio.loadmat(osp.join(
            self._suncg_dir, 'scene_voxels', house_name, view_id + '_voxels.mat'))
        scene_voxels = render_utils.downsample(
            scene_voxels['sceneVox'].astype(np.float32),
            64//self.voxels_size[1], use_max=True)
        return scene_voxels
    
    def forward_affinity(self, select_node_ids1, select_node_ids2):
        """
        Calculate affinity matrix according to select_node_ids
        """
        affinity = np.zeros((self.max_rois, self.max_rois), dtype=np.float32)
        for i, node_id1 in enumerate(select_node_ids1):
            for j, node_id2 in enumerate(select_node_ids2):
                if node_id1 == node_id2:
                    affinity[i, j] = 1
                    #affinity[j, i] = 1
        return affinity

    def __len__(self):
        return self.n_imgs
    
    def __getitem__(self, index):
        house, view1, view2 = self._data_tuples[index]
        valid_1, elem1 = self.forward_single_item(house, view1)
        valid_2, elem2 = self.forward_single_item(house, view2)
        valid = valid_1 and valid_2
        #pdb.set_trace()
        affinity = self.forward_affinity(elem1['select_node_ids'], 
                                         elem2['select_node_ids'])
        
        
        return {
            'valid': valid,
            'views': [elem1, elem2],
            'gt_affinity': affinity,
        }

    def forward_single_item(self, house, view_id):
        if self.output_fine_img:
            img, img_fine, house_name, view_id = self.forward_img(house, view_id)
        else:
            img, house_name, view_id = self.forward_img(house, view_id)

        # print('Starting {} {}_{}, {}'.format(str(datetime.now()), house_name, view_id, multiprocessing.current_process()))
        # sys.stdout.flush()
        # print('{}_{}'.format(house_name, view_id))
        elem = {
            'img': img,
            'house_name': house_name,
            'view_id': view_id,
        }

        if self.output_layout:
            layout = self.forward_layout(house_name, view_id)
            elem['layout'] = layout

        if self.output_voxels:
            voxels = self.forward_voxels(house_name, view_id)
            elem['voxels'] = voxels

        if self.output_codes:
            valid = True
            codes_gt, bboxes_gt, select_node_ids = self.forward_codes(house_name, view_id)
            elem['codes'] = codes_gt
            elem['bboxes'] = bboxes_gt
            elem['select_node_ids'] = select_node_ids
            
            # valid = len(elem['bboxes']) > 0
            if len(elem['bboxes']) == 0: # Ensures that every images has some-information to be help in the loss.
                elem['bboxes'] = []
                valid = False

        if self.output_proposals:
            valid = True
            codes_proposals, bboxes_proposals, labels_proposals = self.forward_proposals(
                house_name, view_id, codes_gt, bboxes_gt)
            if labels_proposals.size == 0:
                # print('No proposal found: ', house_name, view_id, labels_proposals, bboxes_proposals)
                bboxes_proposals = []
                labels_proposals = []
                valid = False
            elem['codes_proposals'] = codes_proposals
            elem['bboxes_proposals'] = bboxes_proposals
            elem['labels_proposals'] = labels_proposals
            
        if self.output_test_proposals:
            bboxes_proposals = self.forward_test_proposals(house_name, view_id)
            if bboxes_proposals.size == 0:
                print('No proposal found: ', house_name, view_id)
                bboxes_proposals = []
                valid = False

            elem['bboxes_test_proposals'] = bboxes_proposals
            
        if self.output_fine_img:
            elem['img_fine'] = img_fine
        return (valid, elem)


#-------- Collate Function --------#
#----------------------------------#    
def recursive_convert_to_torch(elem):
    if torch.is_tensor(elem):
        return elem
    elif type(elem).__module__ == 'numpy':
        if elem.size == 0:
            return torch.zeros(elem.shape).type(torch.DoubleTensor)
        else:
            return torch.from_numpy(elem)
    elif isinstance(elem, int):
        return torch.LongTensor([elem])
    elif isinstance(elem, float):
        return torch.DoubleTensor([elem])
    elif isinstance(elem, collections.Mapping):
        return {key: recursive_convert_to_torch(elem[key]) for key in elem}
    elif isinstance(elem, collections.Sequence):
        return [recursive_convert_to_torch(samples) for samples in elem]
    elif elem is None:
        return elem
    else:
        return elem

def collate_fn(batch):
    '''SUNCG data collater.
    
    Assumes each instance is a dict.
    Applies different collation rules for each field.

    Args:
        batch: List of loaded elements via Dataset.__getitem__
    '''
    collated_batch = {'empty' : True}
    
    # verify valid and collect affinity
    new_batch = []
    affinity = []
    for item in batch:
        valid = item['valid'] 
        t = item['views']
        a = item['gt_affinity']
        if valid:
            new_batch.append(t)
            affinity.append(a)
        else:
            'Print, found a empty in the batch'
    batch = new_batch
    if len(batch) <= 0:
        return collated_batch
    
    collated_batch['gt_affinity'] = default_collate(affinity)
    
    # convert batch
    if len(batch) > 0:
        # treat two views separately
        views = []
        for vid, view in enumerate(batch[0]):
            collated_view = {}
            for key in view:
                if key =='codes' or key=='bboxes' or key=='codes_proposals' \
                        or key=='bboxes_proposals' or key=='bboxes_test_proposals':
                    collated_view[key] = [recursive_convert_to_torch(elem[vid][key]) for elem in batch]
                elif key == 'labels_proposals':
                    collated_view[key] = torch.cat([default_collate(elem[vid][key]) for elem in batch if elem[vid][key].size > 0])
                elif key == 'select_node_ids':  # skip
                    pass
                else:
                    collated_view[key] = default_collate([elem[vid][key] for elem in batch])
            views.append(collated_view)
        
        # concat
        concat_views = {}
        for key in views[0]:
            if key in ['codes', 'bboxes', 'codes_proposals', 'bboxes_proposals',
                       'bboxes_test_proposals', 'house_name', 'view_id']:
                concat_views[key] = list(itertools.chain.from_iterable(view[key] for view in views))
            elif key in ['img', 'img_fine', 'labels_proposals']:
                concat_views[key] = torch.cat(tuple(view[key] for view in views), dim=0)
            else:
                raise ValueError
        
        collated_batch['views'] = concat_views
        collated_batch['empty'] = False
        
    return collated_batch

#----------- Data Loader ----------#
#----------------------------------#
def suncg_data_loader(house_names, opts):
    dset = SuncgDataset(house_names, opts)
    #sampler = RandomSampler(dset, replacement=True, num_samples=1200 * 20)
    sampler = RandomSampler(dset, replacement=True, num_samples=120 * 20)
    return DataLoader(
        dset, batch_size=opts.batch_size, sampler=sampler,
        #shuffle=True, 
        num_workers=opts.n_data_workers,
        collate_fn=collate_fn, pin_memory=True)


def suncg_data_loader_benchmark(house_names, opts):
    dset = SuncgDataset(house_names, opts)
    return DataLoader(
        dset, batch_size=opts.batch_size,
        shuffle=False, num_workers=opts.n_data_workers,
        collate_fn=collate_fn, pin_memory=True)


def define_spatial_image(img_height, img_width, spatial_scale):
    img_height = int(img_height * spatial_scale)
    img_width = int(img_width * spatial_scale)
    spatial_h = torch.arange(0, img_height).unsqueeze(1).expand(torch.Size([img_height, img_width]))
    spatial_w = torch.arange(0, img_width).unsqueeze(0).expand(torch.Size([img_height, img_width]))
    spatial_h = spatial_h.float()
    spatial_w = spatial_w.float()
    spatial_h /= img_height
    spatial_w /= img_width
    spatial_image = torch.stack([spatial_h, spatial_w])
    return spatial_image
