import torch
# import torch should be first. Unclear issue, mentioned here: https://github.com/pytorch/pytorch/issues/2083
import os
import argparse
from pathlib import Path # TODO Use Path instead of os where possible. Better cross-platform compatibility
import csv
import time
import h5py
import datetime
import warnings
import functools

from tqdm import tqdm
from collections import OrderedDict
import shutil
import numpy as np


try:
    from pynvml import *
except ModuleNotFoundError:
    warnings.warn(f"The python Nvidia management library could not be imported. Ignore if running on CPU only.")

import torchvision
import torch.optim as optim
from torch import nn
from torch.utils.data import DataLoader
from torchvision import transforms
from PIL import Image

from utils import augmentation as aug, CreateDataset
from utils.optimizer import create_optimizer
from utils.logger import InformationLogger, save_logs_to_bucket, tsv_line
from utils.metrics import report_classification, create_metrics_dict
from models.model_choice import net, load_checkpoint
from losses import MultiClassCriterion
from utils.utils import load_from_checkpoint, list_s3_subfolders, get_device_ids, gpu_stats, \
    get_key_def
from utils.visualization import vis, vis_from_batch
from utils.readers import read_parameters

try:
    import boto3
except ModuleNotFoundError:
    warnings.warn('The boto3 library counldn\'t be imported. Ignore if not using AWS s3 buckets', ImportWarning)
    pass


def verify_weights(num_classes, weights):
    """Verifies that the number of weights equals the number of classes if any are given
    Args:
        num_classes: number of classes defined in the configuration file
        weights: weights defined in the configuration file
    """
    if num_classes == 1 and len(weights) == 2:
        warnings.warn("got two class weights for single class defined in configuration file; will assume index 0 = background")
    elif num_classes != len(weights):
        raise ValueError('The number of class weights in the configuration file is different than the number of classes')


def flatten_labels(annotations):
    """Flatten labels"""
    flatten = annotations.view(-1)
    return flatten


def flatten_outputs(predictions, number_of_classes):
    """Flatten the prediction batch except the prediction dimensions"""
    logits_permuted = predictions.permute(0, 2, 3, 1)
    logits_permuted_cont = logits_permuted.contiguous()
    outputs_flatten = logits_permuted_cont.view(-1, number_of_classes)
    return outputs_flatten


def loader(path):
    img = Image.open(path)
    return img


def get_s3_classification_images(dataset, bucket, bucket_name, data_path, output_path, num_classes):
    classes = list_s3_subfolders(bucket_name, os.path.join(data_path, dataset))
    classes.sort()
    assert num_classes == len(classes), "The configuration file specified %d classes, but only %d class folders were " \
                                        "found in %s." % (num_classes, len(classes), os.path.join(data_path, dataset))
    with open(os.path.join(output_path, 'classes.csv'), 'wt') as myfile:
        wr = csv.writer(myfile)
        wr.writerow(classes)

    path = os.path.join('Images', dataset)
    try:
        os.mkdir(path) # TODO use Path from pathlib instead?
    except FileExistsError:
        pass
    for c in classes:
        classpath = os.path.join(path, c)
        try:
            os.mkdir(classpath)
        except FileExistsError:
            pass
        for f in bucket.objects.filter(Prefix=os.path.join(data_path, dataset, c)):
            if f.key != data_path + '/':
                bucket.download_file(f.key, os.path.join(classpath, f.key.split('/')[-1]))


def get_local_classes(num_classes, data_path, output_path):
    # Get classes locally and write to csv in output_path
    classes = next(os.walk(os.path.join(data_path, 'trn')))[1]
    classes.sort()
    assert num_classes == len(classes), "The configuration file specified %d classes, but only %d class folders were " \
                                        "found in %s." % (num_classes, len(classes), os.path.join(data_path, 'trn'))
    with open(os.path.join(output_path, 'classes.csv'), 'w') as myfile:
        wr = csv.writer(myfile)
        wr.writerow(classes)


def download_s3_files(bucket_name, data_path, output_path, num_classes, task):
    """
    Function to download the required training files from s3 bucket and sets ec2 paths.
    :param bucket_name: (str) bucket in which data is stored if using AWS S3
    :param data_path: (str) EC2 file path of the folder containing h5py files
    :param output_path: (str) EC2 file path in which the model will be saved
    :param num_classes: (int) number of classes
    :param task: (str) classification or segmentation
    :return: (S3 object) bucket, (str) bucket_output_path, (str) local_output_path, (str) data_path
    """
    bucket_output_path = output_path
    local_output_path = 'output_path'
    try:
        os.mkdir(output_path)
    except FileExistsError:
        pass
    s3 = boto3.resource('s3')
    bucket = s3.Bucket(bucket_name)

    if task == 'classification':
        for i in ['trn', 'val', 'tst']:
            get_s3_classification_images(i, bucket, bucket_name, data_path, output_path, num_classes)
            class_file = os.path.join(output_path, 'classes.csv')
            bucket.upload_file(class_file, os.path.join(bucket_output_path, 'classes.csv'))
        data_path = 'Images'

    elif task == 'segmentation':
        if data_path:
            bucket.download_file(os.path.join(data_path, 'samples/trn_samples.hdf5'),
                                 'samples/trn_samples.hdf5')
            bucket.download_file(os.path.join(data_path, 'samples/val_samples.hdf5'),
                                 'samples/val_samples.hdf5')
            bucket.download_file(os.path.join(data_path, 'samples/tst_samples.hdf5'),
                                 'samples/tst_samples.hdf5')
    else:
        raise ValueError(f"The task should be either classification or segmentation. The provided value is {task}")

    return bucket, bucket_output_path, local_output_path, data_path


def create_dataloader(data_path, batch_size, task, num_devices, samples_folder, params):
    """
    Function to create dataloader objects for training, validation and test datasets.
    :param data_path: (str) path to the samples folder
    :param batch_size: (int) batch size
    :param task: (str) classification or segmentation
    :param num_devices: (int) number of GPUs used
    :param params: (dict) Parameters found in the yaml config file.
    :return: trn_dataloader, val_dataloader, tst_dataloader
    """
    if task == 'classification':
        num_samples = {}
        trn_dataset = torchvision.datasets.ImageFolder(os.path.join(data_path, "trn"),
                                                       transform=transforms.Compose(
                                                           [transforms.RandomRotation((0, 275)),
                                                            transforms.RandomHorizontalFlip(),
                                                            transforms.Resize(299), transforms.ToTensor()]),
                                                       loader=loader)
        val_dataset = torchvision.datasets.ImageFolder(os.path.join(data_path, "val"),
                                                       transform=transforms.Compose(
                                                           [transforms.Resize(299), transforms.ToTensor()]),
                                                       loader=loader)
        tst_dataset = torchvision.datasets.ImageFolder(os.path.join(data_path, "tst"),
                                                       transform=transforms.Compose(
                                                           [transforms.Resize(299), transforms.ToTensor()]),
                                                       loader=loader)
        num_samples['tst'] = len([f for f in Path(data_path).joinpath('tst').glob('**/*')]) #FIXME assert that f is a file
    elif task == 'segmentation':
        assert Path(samples_folder).is_dir(), f'Could not locate: {samples_folder}'
        assert len([f for f in Path(samples_folder).glob('**/*.hdf5')]) >= 1, f"Couldn't locate .hdf5 files in {samples_folder}"
        num_samples = get_num_samples(samples_path=samples_folder, params=params)
        print(f"Number of samples : {num_samples}\n\n")
        meta_map = get_key_def("meta_map", params["global"], {})
        if not meta_map:
            dataset_constr = CreateDataset.SegmentationDataset
        else:
            dataset_constr = functools.partial(CreateDataset.MetaSegmentationDataset, meta_map=meta_map)
        dontcare = get_key_def("ignore_index", params["training"], None)
        if dontcare == 0:
            warnings.warn("The 'dontcare' value (or 'ignore_index') used in the loss function cannot be zero;"
                          " all valid class indices should be consecutive, and start at 0. The 'dontcare' value"
                          " will be remapped to -1 while loading the dataset, and inside the config from now on.")
            params["training"]["ignore_index"] = -1
        datasets = []

        for subset in ["trn", "val", "tst"]:
            datasets.append(dataset_constr(samples_folder, subset,
                                           max_sample_count=num_samples[subset],
                                           dontcare=dontcare,
                                           transform=aug.compose_transforms(params, subset)))
        trn_dataset, val_dataset, tst_dataset = datasets
    else:
        raise ValueError(f"The task should be either classification or segmentation. The provided value is {task}")

    # https://discuss.pytorch.org/t/guidelines-for-assigning-num-workers-to-dataloader/813/5
    num_workers = num_devices * 4 if num_devices > 1 else 4

    # Shuffle must be set to True.
    trn_dataloader = DataLoader(trn_dataset, batch_size=batch_size, num_workers=num_workers, shuffle=True, drop_last=True)
    val_dataloader = DataLoader(val_dataset, batch_size=batch_size, num_workers=num_workers, shuffle=False, drop_last=True)
    tst_dataloader = DataLoader(tst_dataset, batch_size=batch_size, num_workers=num_workers, shuffle=False, drop_last=True) if num_samples['tst'] > 0 else None

    return trn_dataloader, val_dataloader, tst_dataloader


def get_num_samples(samples_path, params):
    """
    Function to retrieve number of samples, either from config file or directly from hdf5 file.
    :param samples_path: (str) Path to samples folder
    :param params: (dict) Parameters found in the yaml config file.
    :return: (dict) number of samples for trn, val and tst.
    """
    num_samples = {'trn': 0, 'val': 0, 'tst': 0}

    for i in ['trn', 'val', 'tst']:
        if params['training'][f"num_{i}_samples"]:
            num_samples[i] = params['training'][f"num_{i}_samples"]

            with h5py.File(os.path.join(samples_path, f"{i}_samples.hdf5"), 'r') as hdf5_file:
                file_num_samples = len(hdf5_file['map_img'])
            if num_samples[i] > file_num_samples:
                raise IndexError(f"The number of training samples in the configuration file ({num_samples[i]}) "
                                 f"exceeds the number of samples in the hdf5 training dataset ({file_num_samples}).")
        else:
            with h5py.File(os.path.join(samples_path, f"{i}_samples.hdf5"), "r") as hdf5_file:
                num_samples[i] = len(hdf5_file['map_img'])

    return num_samples


def set_hyperparameters(params, num_class_w_backr, model, checkpoint):
    """
    Function to set hyperparameters based on values provided in yaml config file.
    Will also set model to GPU, if available.
    If none provided, default functions values may be used.
    :param params: (dict) Parameters found in the yaml config file
    :param model: Model loaded from model_choice.py
    :param checkpoint: (dict) state dict as loaded by model_choice.py
    :return: model, criterion, optimizer, lr_scheduler, num_gpus
    """
    # set mandatory hyperparameters values with those in config file if they exist
    lr = get_key_def('learning_rate', params['training'], None, "missing mandatory learning rate parameter")
    weight_decay = get_key_def('weight_decay', params['training'], None, "missing mandatory weight decay parameter")
    step_size = get_key_def('step_size', params['training'], None, "missing mandatory step size parameter")
    gamma = get_key_def('gamma', params['training'], None, "missing mandatory gamma parameter")

    # optional hyperparameters. Set to None if not in config file
    class_weights = torch.tensor(params['training']['class_weights']) if params['training']['class_weights'] else None
    if params['training']['class_weights']:
        verify_weights(num_class_w_backr, class_weights)
    ignore_index = get_key_def('ignore_index', params['training'], -1)

    # Loss function
    criterion = MultiClassCriterion(loss_type=params['training']['loss_fn'], ignore_index=ignore_index, weight=class_weights)

    # Optimizer
    opt_fn = params['training']['optimizer']
    optimizer = create_optimizer(params=model.parameters(), mode=opt_fn, base_lr=lr, weight_decay=weight_decay)
    lr_scheduler = optim.lr_scheduler.StepLR(optimizer=optimizer, step_size=step_size, gamma=gamma)

    if checkpoint:
        model, optimizer = load_from_checkpoint(checkpoint, model, optimizer=optimizer)

    return model, criterion, optimizer, lr_scheduler


def main(params, config_path):
    """
    Function to train and validate a models for semantic segmentation or classification.
    :param params: (dict) Parameters found in the yaml config file.
    :param config_path: (str) Path to the yaml config file.

    """
    now = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M")

    num_classes = params['global']['num_classes']
    num_classes_w_backgr = num_classes + 1 # + 1 for background
    model, checkpoint, model_name = net(params, num_classes_w_backgr)
    bucket_name = params['global']['bucket_name']
    data_path = params['global']['data_path']

    samples_size = params["global"]["samples_size"]  # FIXME: provide to function as parameters?
    overlap = params["sample"]["overlap"]
    min_annot_perc = params['sample']['min_annotated_percent']
    samples_folder_name = f'{samples_size}x{samples_size}samp_{overlap}overlap_{min_annot_perc}min-annot'  # FIXME: document!
    samples_folder = Path(data_path).joinpath(samples_folder_name)

    modelname = config_path.stem
    output_path = Path(samples_folder).joinpath('model') / modelname
    if output_path.is_dir():
        output_path = Path(str(output_path)+'_'+now)
    output_path.mkdir(parents=True, exist_ok=False)
    shutil.copy(str(config_path), str(output_path))
    tqdm.write(f'Model and log files will be saved to: {output_path}\n\n')
    task = params['global']['task']
    batch_size = params['training']['batch_size']

    if bucket_name:
        bucket, bucket_output_path, output_path, data_path = download_s3_files(bucket_name=bucket_name,
                                                                               data_path=data_path,
                                                                               output_path=output_path,
                                                                               num_classes=num_classes_w_backgr,
                                                                               task=task)

    elif not bucket_name and task == 'classification':
        get_local_classes(num_classes, data_path, output_path)

    since = time.time()
    best_loss = 999

    progress_log = Path(output_path) / 'progress.log'
    if not progress_log.exists():
        progress_log.open('w', buffering=1).write(tsv_line('ep_idx', 'phase', 'iter', 'i_p_ep', 'time'))    # Add header

    trn_log = InformationLogger(output_path, 'trn')
    val_log = InformationLogger(output_path, 'val')
    tst_log = InformationLogger(output_path, 'tst')

    num_devices = params['global']['num_gpus']
    assert num_devices is not None and num_devices >= 0, "missing mandatory num gpus parameter"
    # list of GPU devices that are available and unused. If no GPUs, returns empty list
    lst_device_ids = get_device_ids(num_devices) if torch.cuda.is_available() else []
    num_devices = len(lst_device_ids) if lst_device_ids else 0
    device = torch.device(f'cuda:{lst_device_ids[0]}' if torch.cuda.is_available() and lst_device_ids else 'cpu')
    print(f"Number of cuda devices requested: {params['global']['num_gpus']}. Cuda devices available: {lst_device_ids}")
    if num_devices == 1:
        print(f"Using Cuda device {lst_device_ids[0]}")
    elif num_devices > 1:
        print(f"Using data parallel on devices: {str(lst_device_ids)[1:-1]}. Main device: {lst_device_ids[0]}\n\n") # FIXME: why are we showing indices [1:-1] for lst_device_ids?
        try: # For HPC when device 0 not available. Error: Invalid device id (in torch/cuda/__init__.py).
            model = nn.DataParallel(model, device_ids=lst_device_ids)  # DataParallel adds prefix 'module.' to state_dict keys
        except AssertionError:
            warnings.warn(f"Unable to use devices {lst_device_ids}. Trying devices {list(range(len(lst_device_ids)))}\n\n")
            device = torch.device('cuda:0')
            lst_device_ids = range(len(lst_device_ids))
            model = nn.DataParallel(model,
                                    device_ids=lst_device_ids)  # DataParallel adds prefix 'module.' to state_dict keys

    else:
        warnings.warn(f"No Cuda device available. This process will only run on CPU")

    trn_dataloader, val_dataloader, tst_dataloader = create_dataloader(data_path=data_path,
                                                                       batch_size=batch_size,
                                                                       task=task,
                                                                       num_devices=num_devices,
                                                                       samples_folder=samples_folder,
                                                                       params=params)

    model, criterion, optimizer, lr_scheduler = set_hyperparameters(params, num_classes_w_backgr, model, checkpoint)

    criterion = criterion.to(device)
    try: # For HPC when device 0 not available. Error: Cuda invalid device ordinal.
        model.to(device)
    except RuntimeError:
        warnings.warn(f"Unable to use device. Trying device 0")
        device = torch.device(f'cuda:0' if torch.cuda.is_available() and lst_device_ids else 'cpu')
        model.to(device)

    filename = os.path.join(output_path, 'checkpoint.pth.tar')

    # VISUALIZATION: generate png of test samples, labels and outputs for visualisation to follow training performance
    vis_at_init = get_key_def('vis_at_init', params['visualization'], False)
    max_num_vis_samples = get_key_def('max_num_vis_samples', params['visualization'], 24)
    if vis_at_init and task == 'segmentation':
        tqdm.write(f'Visualizing initialized model on {max_num_vis_samples} test samples...\n\n')
        vis_from_dataloader(params=params,
                            eval_loader=val_dataloader,
                            model=model,
                            ep_num=0,
                            output_path=output_path,
                            dataset='val',
                            device=device,
                            max_num_samples=max_num_vis_samples)

    for epoch in range(0, params['training']['num_epochs']):
        print(f'\nEpoch {epoch}/{params["training"]["num_epochs"] - 1}\n{"-" * 20}')

        trn_report = train(train_loader=trn_dataloader,
                           model=model,
                           criterion=criterion,
                           optimizer=optimizer,
                           scheduler=lr_scheduler,
                           num_classes=num_classes_w_backgr,
                           batch_size=batch_size,
                           task=task,
                           ep_idx=epoch,
                           progress_log=progress_log,
                           vis_params=params,
                           device=device)
        trn_log.add_values(trn_report, epoch, ignore=['precision', 'recall', 'fscore', 'iou'])


        val_report = evaluation(eval_loader=val_dataloader,
                                model=model,
                                criterion=criterion,
                                num_classes=num_classes_w_backgr,
                                batch_size=batch_size,
                                task=task,
                                ep_idx=epoch,
                                progress_log=progress_log,
                                vis_params=params,
                                batch_metrics=params['training']['batch_metrics'],
                                dataset='val',
                                device=device)
        val_loss = val_report['loss'].avg
        if params['training']['batch_metrics'] is not None:
            val_log.add_values(val_report, epoch)
        else:
            val_log.add_values(val_report, epoch, ignore=['precision', 'recall', 'fscore', 'iou'])

        if val_loss < best_loss:
            print("save checkpoint")
            best_loss = val_loss
            # More info: https://pytorch.org/tutorials/beginner/saving_loading_models.html#saving-torch-nn-dataparallel-models
            state_dict = model.module.state_dict() if num_devices > 1 else model.state_dict()
            torch.save({'epoch': epoch,
                        'arch': model_name,
                        'model': state_dict,
                        'best_loss': best_loss,
                        'optimizer': optimizer.state_dict()}, filename)

            if bucket_name:
                bucket_filename = os.path.join(bucket_output_path, 'checkpoint.pth.tar')
                bucket.upload_file(filename, bucket_filename)

            # VISUALIZATION: generate png of test samples, labels and outputs for visualisation to follow training performance
            vis_at_checkpoint = get_key_def('vis_at_checkpoint', params['visualization'], False)
            ep_vis_min_thresh = get_key_def('vis_at_ckpt_min_ep_diff', params['visualization'], 4) # FIXME: document this in README
            max_num_vis_samples = get_key_def('max_num_vis_samples', params['visualization'], 24)
            last_vis_epoch = 0
            if vis_at_checkpoint and epoch - last_vis_epoch >= ep_vis_min_thresh and task == 'segmentation':
                tqdm.write(f'Visualizing on {max_num_vis_samples} test samples...')
                vis_from_dataloader(params=params,
                                    eval_loader=val_dataloader,
                                    model=model,
                                    ep_num=epoch+1,
                                    output_path=output_path,
                                    dataset='val',
                                    device=device,
                                    max_num_samples=max_num_vis_samples)
                last_vis_epoch = epoch
            else:
                warnings.warn(f'Visualization is currently only implemented for 5-class semantic segmentation tasks')

        if bucket_name:
            save_logs_to_bucket(bucket, bucket_output_path, output_path, now, params['training']['batch_metrics'])

        cur_elapsed = time.time() - since
        print(f'Current elapsed time {cur_elapsed // 60:.0f}m {cur_elapsed % 60:.0f}s')

    # load checkpoint model and evaluate it on test dataset.
    if int(params['training']['num_epochs']) > 0:    #if num_epochs is set to 0, model is loaded to evaluate on test set
        checkpoint = load_checkpoint(filename)
        model, _ = load_from_checkpoint(checkpoint, model)

    if tst_dataloader:
        tst_report = evaluation(eval_loader=tst_dataloader,
                            model=model,
                            criterion=criterion,
                            num_classes=num_classes_w_backgr,
                            batch_size=batch_size,
                            task=task,
                            ep_idx=params['training']['num_epochs'],
                            progress_log=progress_log,
                            vis_params=params,
                            batch_metrics=params['training']['batch_metrics'],
                            dataset='tst',
                            device=device)
        tst_log.add_values(tst_report, params['training']['num_epochs'])

        if bucket_name:
            bucket_filename = os.path.join(bucket_output_path, 'last_epoch.pth.tar')
            bucket.upload_file("output.txt", os.path.join(bucket_output_path, f"Logs/{now}_output.txt"))
            bucket.upload_file(filename, bucket_filename)

    time_elapsed = time.time() - since
    print('Training complete in {:.0f}m {:.0f}s'.format(time_elapsed // 60, time_elapsed % 60))


def train(train_loader, model, criterion, optimizer, scheduler, num_classes, batch_size, task, ep_idx, progress_log, vis_params, device, debug=False):
    """
    Train the model and return the metrics of the training epoch
    :param train_loader: training data loader
    :param model: model to train
    :param criterion: loss criterion
    :param optimizer: optimizer to use
    :param scheduler: learning rate scheduler
    :param num_classes: number of classes
    :param batch_size: number of samples to process simultaneously
    :param task: segmentation or classification
    :param ep_idx: epoch index (for hypertrainer log)
    :param progress_log: progress log file (for hypertrainer log)
    :param vis_params: #FIXME document
    :param device: device used by pytorch (cpu ou cuda)
    :return: Updated training loss
    """
    model.train()
    train_metrics = create_metrics_dict(num_classes)

    with tqdm(train_loader, desc=f'Iterating batches with {device.type}') as _tqdm:
        for batch_index, data in enumerate(_tqdm):
            progress_log.open('a', buffering=1).write(tsv_line(ep_idx, 'trn', batch_index, len(train_loader), time.time()))

            if task == 'classification':
                inputs, labels = data
                inputs = inputs.to(device)
                labels = labels.to(device)
                optimizer.zero_grad()
                outputs = model(inputs)
            elif task == 'segmentation':
                inputs = data['sat_img'].to(device)
                labels = data['map_img'].to(device)

                # forward
                optimizer.zero_grad()
                outputs = model(inputs)
                # added for torchvision models that output an OrderedDict with outputs in 'out' key.
                # More info: https://pytorch.org/hub/pytorch_vision_deeplabv3_resnet101/
                if isinstance(outputs, OrderedDict):
                    outputs = outputs['out']

                if get_key_def('vis_at_train', vis_params['visualization'], False):
                    max_num_vis_samples = get_key_def('max_num_vis_samples', vis_params['visualization'], False)
                    max_batch_vis = round(max_num_vis_samples / batch_size)
                    vis_path = progress_log.parent.joinpath('visualization')
                    if ep_idx == 0:
                        tqdm.write(f'Visualizing on train outputs for max {max_batch_vis} batches. All images will be saved to {vis_path}\n\n')
                    if batch_index < max_batch_vis:
                        vis_from_batch(params, inputs, labels, outputs,
                                       batch_index=batch_index,
                                       vis_path=vis_path,
                                       dataset='trn',
                                       ep_num=ep_idx+1)

            loss = criterion(outputs, labels)

            train_metrics['loss'].update(loss.item(), batch_size)

            if device.type == 'cuda' and debug: #FIXME: not showing
                res, mem = gpu_stats(device=device.index)
                _tqdm.set_postfix(OrderedDict(trn_loss=f'{train_metrics["loss"].val:.2f}',
                                              gpu_perc=f'{res.gpu} %',
                                              gpu_RAM=f'{mem.used / (1024 ** 2):.0f}/{mem.total / (1024 ** 2):.0f} MiB',
                                              lr=optimizer.param_groups[0]['lr'],
                                              img=data['sat_img'].numpy().shape[1:],
                                              smpl=data['map_img'].numpy().shape,
                                              bs=batch_size,
                                              out_vals=np.unique(outputs[0].argmax(dim=0).detach().cpu().numpy())))

            loss.backward()
            optimizer.step()

    scheduler.step()
    print(f'Training Loss: {train_metrics["loss"].avg:.4f}')
    return train_metrics


def evaluation(eval_loader, model, criterion, num_classes, batch_size, task, ep_idx, progress_log, vis_params, batch_metrics=None, dataset='val', device=None):
    """
    Evaluate the model and return the updated metrics
    :param eval_loader: data loader
    :param model: model to evaluate
    :param criterion: loss criterion
    :param num_classes: number of classes
    :param batch_size: number of samples to process simultaneously
    :param task: segmentation or classification
    :param ep_idx: epoch index (for hypertrainer log)
    :param progress_log: progress log file (for hypertrainer log)
    :param batch_metrics: (int) Metrics computed every (int) batches. If left blank, will not perform metrics.
    :param dataset: (str) 'val or 'tst'
    :param device: device used by pytorch (cpu ou cuda)
    :return: (dict) eval_metrics
    """
    eval_metrics = create_metrics_dict(num_classes)
    model.eval()

    with tqdm(eval_loader, dynamic_ncols=True, desc='Iterating batch') as _tqdm:
        for batch_index, data in enumerate(_tqdm):
            progress_log.open('a', buffering=1).write(tsv_line(ep_idx, dataset, batch_index, len(eval_loader), time.time()))

            with torch.no_grad():
                if task == 'classification':
                    inputs, labels = data
                    inputs = inputs.to(device)
                    labels = labels.to(device)
                    labels_flatten = labels

                    outputs = model(inputs)
                    outputs_flatten = outputs
                elif task == 'segmentation':
                    inputs = data['sat_img'].to(device)
                    labels = data['map_img'].to(device)
                    labels_flatten = flatten_labels(labels)

                    outputs = model(inputs)
                    if isinstance(outputs, OrderedDict):
                        outputs = outputs['out']

                    if get_key_def('vis_at_evaluation', vis_params['visualization'], False):
                        max_num_vis_samples = get_key_def('max_num_vis_samples', vis_params['visualization'], False)
                        max_batch_vis = round(max_num_vis_samples/batch_size)
                        vis_path = progress_log.parent.joinpath('visualization')
                        if ep_idx == 0:
                            tqdm.write(f'Visualizing on {dataset} outputs for max than {max_batch_vis} batches. All images will be saved to {vis_path}\n\n')
                        if batch_index < max_batch_vis:
                            vis_from_batch(params, inputs, labels, outputs,
                                           batch_index=batch_index,
                                           vis_path=vis_path,
                                           dataset=dataset,
                                           ep_num=ep_idx+1)

                    outputs_flatten = flatten_outputs(outputs, num_classes)

                loss = criterion(outputs, labels)

                eval_metrics['loss'].update(loss.item(), batch_size)

                if (dataset == 'val') and (batch_metrics is not None):
                    # Compute metrics every n batches. Time consuming.
                    assert batch_metrics <= len(_tqdm), f"Batch_metrics ({batch_metrics} is smaller than batch size " \
                        f"{len(_tqdm)}. Metrics in validation loop won't be computed"
                    if (batch_index+1) % batch_metrics == 0:   # +1 to skip val loop at very beginning
                        a, segmentation = torch.max(outputs_flatten, dim=1)
                        eval_metrics = report_classification(segmentation, labels_flatten, batch_size, eval_metrics,
                                                             ignore_index=get_key_def("ignore_index", params["training"], None))
                elif dataset == 'tst':
                    a, segmentation = torch.max(outputs_flatten, dim=1)
                    eval_metrics = report_classification(segmentation, labels_flatten, batch_size, eval_metrics,
                                                         ignore_index=get_key_def("ignore_index", params["training"], None))

                _tqdm.set_postfix(OrderedDict(dataset=dataset, loss=f'{eval_metrics["loss"].avg:.4f}'))

                if debug and device.type == 'cuda':
                    res, mem = gpu_stats(device=device.index)
                    _tqdm.set_postfix(OrderedDict(device=device, gpu_perc=f'{res.gpu} %',
                                                  gpu_RAM=f'{mem.used/(1024**2):.0f}/{mem.total/(1024**2):.0f} MiB'))

    print(f"{dataset} Loss: {eval_metrics['loss'].avg}")
    if batch_metrics is not None:
        print(f"{dataset} precision: {eval_metrics['precision'].avg}")
        print(f"{dataset} recall: {eval_metrics['recall'].avg}")
        print(f"{dataset} fscore: {eval_metrics['fscore'].avg}")

    return eval_metrics


def vis_from_dataloader(params, eval_loader, model, ep_num, output_path, dataset='', device=None, max_num_samples=8):
    """
    Create images from output of model
    :param eval_loader: data loader
    :param model: model to evaluate
    :param ep_num: epoch index (for hypertrainer log)
    :param dataset: (str) 'val or 'tst'
    :param output_path: path where inferences on samples will be saved
    :param device: device used by pytorch (cpu ou cuda)
    :param max_num_samples: (int) max number of samples to perform visualization on
    :param heatmaps: (bool) Save heatmaps associated to output, along with input, label and output

    :return:
    """
    assert params['global']['task'] == 'segmentation'
    vis_path = output_path.joinpath(f'visualization')
    tqdm.write(f'Visualization figures will be saved to {vis_path}\n\n')

    model.eval()
    with tqdm(eval_loader, dynamic_ncols=True) as _tqdm:
        for batch_index, data in enumerate(_tqdm):
            with torch.no_grad():
                inputs = data['sat_img'].to(device)
                labels = data['map_img'].to(device)

                outputs = model(inputs)
                if isinstance(outputs, OrderedDict):
                    outputs = outputs['out']

                vis_from_batch(params=params,
                               inputs=inputs,
                               labels=labels,
                               outputs=outputs,
                               batch_index=batch_index,
                               vis_path=vis_path,
                               dataset=dataset,
                               ep_num=ep_num)

                if ((batch_index+1) * eval_loader.batch_size) >= max_num_samples:
                    break
    tqdm.write(f'Saved visualization figures.\n\n')


if __name__ == '__main__':
    print(f'Start\n\n')
    parser = argparse.ArgumentParser(description='Training execution')
    parser.add_argument('param_file', metavar='DIR',
                        help='Path to training parameters stored in yaml')
    args = parser.parse_args()
    config_path = Path(args.param_file)
    params = read_parameters(args.param_file)

    debug = get_key_def('debug_mode', params['global'], False)
    if debug:
        warnings.warn(f'Debug mode activated. Some debug functions may cause delays in execution.')

    main(params, config_path)
    print('End of training')
