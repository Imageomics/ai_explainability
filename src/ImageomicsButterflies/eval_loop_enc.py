﻿# Copyright (c) 2021, NVIDIA CORPORATION.  All rights reserved.
#
# NVIDIA CORPORATION and its licensors retain all intellectual property
# and proprietary rights in and to this software, related documentation
# and any modifications thereto.  Any use, reproduction, disclosure or
# distribution of this software and related documentation without an express
# license agreement from NVIDIA CORPORATION is strictly prohibited.

import os
import time
import copy
import json
import pickle
from typing import OrderedDict
import psutil
import PIL.Image
import numpy as np
import torch
import dnnlib
from torch_utils import misc
from torch_utils import training_stats
from torch_utils.ops import conv2d_gradfix
from torch_utils.ops import grid_sample_gradfix

import sys

import legacy
from metrics import metric_main

from training.our_encoder import Encoder
from idinvert_pytorch.models.stylegan_encoder_network import StyleGANEncoderNet
from torch.utils.data import Subset
from vgg_butterfly.vggs import VGG16
from resnet_finetune_cub.models.models_for_cub import ResNet
import math
from tqdm import tqdm


#----------------------------------------------------------------------------

def setup_snapshot_image_grid(training_set, random_seed=0):
    rnd = np.random.RandomState(random_seed)
    gw = np.clip(7680 // training_set.image_shape[2], 7, 32)
    gh = np.clip(4320 // training_set.image_shape[1], 4, 32)

    # No labels => show random subset of training samples.
    if not training_set.has_labels:
        all_indices = list(range(len(training_set)))
        rnd.shuffle(all_indices)
        grid_indices = [all_indices[i % len(all_indices)] for i in range(gw * gh)]

    else:
        # Group training samples by label.
        label_groups = dict() # label => [idx, ...]
        for idx in range(len(training_set)):
            label = tuple(training_set.get_details(idx).raw_label.flat[::-1])
            if label not in label_groups:
                label_groups[label] = []
            label_groups[label].append(idx)

        # Reorder.
        label_order = sorted(label_groups.keys())
        for label in label_order:
            rnd.shuffle(label_groups[label])

        # Organize into grid.
        grid_indices = []
        for y in range(gh):
            label = label_order[y % len(label_order)]
            indices = label_groups[label]
            grid_indices += [indices[x % len(indices)] for x in range(gw)]
            label_groups[label] = [indices[(i + gw) % len(indices)] for i in range(len(indices))]

    # Load data.
    images, labels = zip(*[training_set[i] for i in grid_indices])
    return (gw, gh), np.stack(images), np.stack(labels)

#----------------------------------------------------------------------------

def save_image_grid(img, fname, drange, grid_size):
    lo, hi = drange
    img = np.asarray(img, dtype=np.float32)
    img = (img - lo) * (255 / (hi - lo))
    img = np.rint(img).clip(0, 255).astype(np.uint8)

    gw, gh = grid_size
    _N, C, H, W = img.shape
    img = img.reshape(gh, gw, C, H, W)
    img = img.transpose(0, 3, 1, 4, 2)
    img = img.reshape(gh * H, gw * W, C)

    assert C in [1, 3]
    if C == 1:
        PIL.Image.fromarray(img[:, :, 0], 'L').save(fname)
    if C == 3:
        PIL.Image.fromarray(img, 'RGB').save(fname)

#----------------------------------------------------------------------------

def save_img_w_our_encoder(org_imgs, enc_imgs, eval_output):
    for i, (org_img, enc_img) in enumerate(zip(org_imgs, enc_imgs)):
        
        #print("org_img_bef: ", org_img.shape)
        
        # change the order of dimensions of image
        # (C, W, H) -> (W,H,C)
        org_img = org_img.permute(1,2,0).clamp(0, 255).to(torch.uint8).cpu().numpy()
        
        enc_img = enc_img.permute(1,2,0).clamp(0, 255).to(torch.uint8).cpu().numpy()

        #print("org_img_shape: ", org_img.shape)
        #print("enc_img_shape: ", enc_img.shape)
        
        org_enc_img = np.hstack((org_img, enc_img))
        #print("eval_output: ", eval_output)
       
        #PIL.Image.fromarray(org_img, 'RGB').save(os.path.join(eval_output, 'org_enc_img_' + str(i) + '.jpg'))
        PIL.Image.fromarray(org_enc_img, 'RGB').save(os.path.join(eval_output, 'org_enc_img_' + str(i) + '.jpg'))


def eval_encoder(our_encoder, test_set, n_source_imgs, img_res, device, G, eval_output, smp_idices, enc_arch):
    print("Evaluating our encoder...")
    # save images generated by our_encoder
    #print("smp_idices: ", smp_idices)

    smp_org_imgs_dset = Subset(test_set, smp_idices)
    smp_org_imgs_iterator = iter(torch.utils.data.DataLoader(dataset=smp_org_imgs_dset, batch_size=n_source_imgs))
    smp_org_imgs, _ = next(smp_org_imgs_iterator)
    smp_org_imgs = smp_org_imgs.to(device).to(torch.float32)
    smp_org_imgs_for_our_encoder = smp_org_imgs / (255/2) - 1

    #print("smp_org_imgs: ", smp_org_imgs.shape)

    our_encoder.eval()
    G.synthesis.eval()
    #G_ema.synthesis.eval()
    with torch.no_grad():
        log_size = int(math.log(img_res, 2))
        n_latent = log_size * 2 - 2
        if enc_arch=='idinvert':
            our_encoder_w = our_encoder(smp_org_imgs_for_our_encoder)
            our_encoder_w = our_encoder_w.unsqueeze(1).repeat(1, n_latent, 1)
        elif enc_arch=='ae_stylegan':
            our_encoder_w, _ = our_encoder(smp_org_imgs_for_our_encoder)
            our_encoder_w = torch.reshape(our_encoder_w, (-1, n_latent, 512))

        print("our_encoder_w: ", our_encoder_w.shape)
        smp_our_encoder_gen_imgs = G.synthesis(our_encoder_w, noise_mode='const')
        smp_our_encoder_gen_imgs = (smp_our_encoder_gen_imgs + 1) * (255/2)
        #print("smp_our_encoder_gen_imgs: ", smp_our_encoder_gen_imgs.shape)
        
    save_img_w_our_encoder(smp_org_imgs, smp_our_encoder_gen_imgs, eval_output)

#----------------------------------------------------------------------------

def eval_loop_enc(
    run_dir                 = '.',      # Output directory.
    training_set_kwargs     = {},       # Options for training set.
    test_set_kwargs         = {},       # Options for test set.
    data_loader_kwargs      = {},       # Options for torch.utils.data.DataLoader.
    G_kwargs                = {},       # Options for generator network.
    D_kwargs                = {},       # Options for discriminator network.
    G_opt_kwargs            = {},       # Options for generator optimizer.
    D_opt_kwargs            = {},       # Options for discriminator optimizer.
    our_encoder_opt_kwargs  = {},       # Options for our_encoder optimizer.
    augment_kwargs          = None,     # Options for augmentation pipeline. None = disable.
    loss_kwargs             = {},       # Options for loss function.
    metrics                 = [],       # Metrics to evaluate during training.
    random_seed             = 0,        # Global random seed.
    num_gpus                = 1,        # Number of GPUs participating in the training.
    rank                    = 0,        # Rank of the current process in [0, num_gpus[.
    batch_size              = 4,        # Total batch size for one training iteration. Can be larger than batch_gpu * num_gpus.
    batch_gpu               = 4,        # Number of samples processed at a time by one GPU.
    ema_kimg                = 10,       # Half-life of the exponential moving average (EMA) of generator weights.
    ema_rampup              = None,     # EMA ramp-up coefficient.
    G_reg_interval          = 4,        # How often to perform regularization for G? None = disable lazy regularization.
    D_reg_interval          = 16,       # How often to perform regularization for D? None = disable lazy regularization.
    augment_p               = 0,        # Initial value of augmentation probability.
    ada_target              = None,     # ADA target value. None = fixed p.
    ada_interval            = 4,        # How often to perform ADA adjustment?
    ada_kimg                = 500,      # ADA adjustment speed, measured in how many kimg it takes for p to increase/decrease by one unit.
    total_kimg              = 25000,    # Total length of the training, measured in thousands of real images.
    kimg_per_tick           = 4,        # Progress snapshot interval.
    image_snapshot_ticks    = 50,       # How often to save image snapshots? None = disable.
    network_snapshot_ticks  = 50,       # How often to save network snapshots? None = disable.
    resume_pkl              = None,     # Network pickle to resume training from.
    cudnn_benchmark         = True,     # Enable torch.backends.cudnn.benchmark?
    allow_tf32              = False,    # Enable torch.backends.cuda.matmul.allow_tf32 and torch.backends.cudnn.allow_tf32?
    abort_fn                = None,     # Callback function for determining whether to abort training. Must return consistent results across ranks.
    progress_fn             = None,     # Callback function for updating training progress. Called for all ranks.
    pix_lambda              = 1.0,      # MSE loss lambda
    n_epochs                = 3,        # number of epochs
    img_res                 = 128,      # image resolution
    n_source_imgs           = 5,        # number of sample images to evaluate our encoder
    percept_lambda          = 5e-5,     # peception loss lambda
    pix_loss_type           = 'l2',      # pixel loss type. L1 or L2
    percept_model_name      = 'vgg_butterfly',     # percept model,
    adv_enc_lambda          = 0.0,        # adv loss lambda for encoder
    enc_arch                = 'ae_stylegan'
):
    # Initialize.
    start_time = time.time()
    device = torch.device('cuda', rank)
    np.random.seed(random_seed * num_gpus + rank)
    torch.manual_seed(random_seed * num_gpus + rank)
    torch.backends.cudnn.benchmark = cudnn_benchmark    # Improves training speed.
    torch.backends.cuda.matmul.allow_tf32 = allow_tf32  # Allow PyTorch to internally use tf32 for matmul
    torch.backends.cudnn.allow_tf32 = allow_tf32        # Allow PyTorch to internally use tf32 for convolutions
    conv2d_gradfix.enabled = True                       # Improves training speed.
    grid_sample_gradfix.enabled = True                  # Avoids errors with the augmentation pipe.

    # Load training set.
    if rank == 0:
        print('Loading training set...')
    training_set = dnnlib.util.construct_class_by_name(**training_set_kwargs) # subclass of training.dataset.Dataset
    training_set_sampler = misc.InfiniteSampler(dataset=training_set, rank=rank, num_replicas=num_gpus, seed=random_seed)
    
    #training_set_iterator = iter(torch.utils.data.DataLoader(dataset=training_set, sampler=training_set_sampler, batch_size=batch_size//num_gpus, **data_loader_kwargs))
    
    if rank == 0:
        print()
        print('Training Num images: ', len(training_set))
        print('Training Image shape:', training_set.image_shape)
        print('Training Label shape:', training_set.label_shape)
        print()

    # Load test set.
    if rank == 0:
        print('Loading test set...')
    test_set = dnnlib.util.construct_class_by_name(**test_set_kwargs) # subclass of test.dataset.Dataset
    test_set_sampler = misc.InfiniteSampler(dataset=test_set, rank=rank, num_replicas=num_gpus, seed=random_seed)
    test_set_iterator = iter(torch.utils.data.DataLoader(dataset=test_set, sampler=test_set_sampler, batch_size=batch_size//num_gpus, **data_loader_kwargs))
    if rank == 0:
        print()
        print('Test Num images: ', len(test_set))
        print('Test Image shape:', test_set.image_shape)
        print('Test Label shape:', test_set.label_shape)
        print()


    # Construct networks.
    if rank == 0:
        print('Constructing networks...')
    common_kwargs = dict(c_dim=training_set.label_dim, img_resolution=training_set.resolution, img_channels=training_set.num_channels)
    G = dnnlib.util.construct_class_by_name(**G_kwargs, **common_kwargs).train().requires_grad_(False).to(device) # subclass of torch.nn.Module
    D = dnnlib.util.construct_class_by_name(**D_kwargs, **common_kwargs).train().requires_grad_(False).to(device) # subclass of torch.nn.Module
    G_ema = copy.deepcopy(G).eval()

    
    if percept_model_name=='vgg_butterfly':
        print("load VGG trained on butterfly...")
        percept_model = VGG16()
        but_cp = torch.load('vgg_butterfly/vgg_backbone_nohybrid.pt')
        percept_model.load_state_dict(but_cp)
        for name, param in percept_model.named_parameters():
            param.requires_grad = False
        percept_model.eval().to(device)
    elif percept_model_name=='vgg_stylegan':
        print("load StyleGAN vgg...")
        # Load VGG16 feature detector.
        url = 'https://nvlabs-fi-cdn.nvidia.com/stylegan2-ada-pytorch/pretrained/metrics/vgg16.pt'
        with dnnlib.util.open_url(url) as f:
            percept_model = torch.jit.load(f).eval().to(device)
    elif percept_model_name=='resnet_cub':
        print("load ResNet trained on CUB...")
         # load a resnet classifer trained on CUB and freeze its all parameters
        percept_model = ResNet().to(device)
        saved_state_dict_module = torch.load("resnet_finetune_cub/results/ResNet/ResNet50_img_res_256_bs_64_lr_0.001_pre_trained_True.pt")
        saved_state_dict_wo_module = {}
        for k, v in saved_state_dict_module.items():
            k_wo_module = k[7:]
            saved_state_dict_wo_module[k_wo_module] = v
        percept_model.load_state_dict(saved_state_dict_wo_module)
        percept_model.only_feature_extractor()
        for param in percept_model.parameters():
            param.requires_grad = False
        
        #for param in percept_model.feature_extractor.parameters():
        #    assert param.requires_grad == False
        #for par1, par2 in zip(percept_model.parameters(), percept_model.feature_extractor.parameters()):
        #    assert par1 is par2
        
    # our_encoder network
    if enc_arch == 'idinvert':
        print("idinvert...")
        our_encoder = StyleGANEncoderNet(resolution=img_res, w_space_dim=512, which_latent='w_shared')
    elif enc_arch == 'ae_stylegan':
        print("ae_stylegan...")
        our_encoder = Encoder(size=img_res)
    our_encoder = our_encoder.to(device)

    our_encoder.load_state_dict(torch.load('butterfly_training-runs_encoder/00000_butterfly128x128-auto1-batch16-resumecustom_epoch_50_pix_loss_type_l1_1.0_perception_loss_0.0_vgg_butterfly_True/network-snapshot-our_encoder.pt'))

    # Resume from existing pickle.
    if (resume_pkl is not None) and (rank == 0):
        print(f'Resuming from "{resume_pkl}"')
        with dnnlib.util.open_url(resume_pkl) as f:
            resume_data = legacy.load_network_pkl(f)
        for name, module in [('G', G), ('D', D), ('G_ema', G_ema)]:
            misc.copy_params_and_buffers(resume_data[name], module, require_all=False)

    G = copy.deepcopy(G_ema).eval()

    # Print network summary tables.
    if rank == 0:
        z = torch.empty([batch_gpu, G.z_dim], device=device)
        c = torch.empty([batch_gpu, G.c_dim], device=device)
        img = misc.print_module_summary(G, [z, c])
        misc.print_module_summary(D, [img, c])

    # Setup augmentation.
    if rank == 0:
        print('Setting up augmentation...')
    augment_pipe = None
    ada_stats = None
    if (augment_kwargs is not None) and (augment_p > 0 or ada_target is not None):
        augment_pipe = dnnlib.util.construct_class_by_name(**augment_kwargs).train().requires_grad_(False).to(device) # subclass of torch.nn.Module
        augment_pipe.p.copy_(torch.as_tensor(augment_p))
        if ada_target is not None:
            ada_stats = training_stats.Collector(regex='Loss/signs/real')

    # Distribute across GPUs.
    if rank == 0:
        print(f'Distributing across {num_gpus} GPUs...')
    ddp_modules = dict()
    for name, module in [('G_mapping', G.mapping), ('G_synthesis', G.synthesis), ('D', D), (None, G_ema), ('augment_pipe', augment_pipe), ('our_encoder', our_encoder)]:
        if name is not None:
            
            if adv_enc_lambda > 0.0:
                # freeze generator
                if not (name=='our_encoder' or name=='D'):
                    module.requires_grad_(False)
                else:
                    module.requires_grad_(True)
            else:
                # Freeze all networks except for our_encoder
                if not name=='our_encoder':
                    module.requires_grad_(False)
                else:
                    module.requires_grad_(True)
            ddp_modules[name] = module

    # Setup training phases.
    if rank == 0:
        print('Setting up training phases...')
    loss = dnnlib.util.construct_class_by_name(device=device, **ddp_modules, **loss_kwargs) # subclass of training.loss.Loss
    print("loss: ", loss)
    phases = []
    for name, module, opt_kwargs, reg_interval in [('G', G, G_opt_kwargs, G_reg_interval), ('D', D, D_opt_kwargs, D_reg_interval)]:
        if reg_interval is None:
            opt = dnnlib.util.construct_class_by_name(params=module.parameters(), **opt_kwargs) # subclass of torch.optim.Optimizer
            phases += [dnnlib.EasyDict(name=name+'both', module=module, opt=opt, interval=1)]
        else: # Lazy regularization.
            mb_ratio = reg_interval / (reg_interval + 1)
            opt_kwargs = dnnlib.EasyDict(opt_kwargs)
            opt_kwargs.lr = opt_kwargs.lr * mb_ratio
            opt_kwargs.betas = [beta ** mb_ratio for beta in opt_kwargs.betas]
            opt = dnnlib.util.construct_class_by_name(module.parameters(), **opt_kwargs) # subclass of torch.optim.Optimizer
            
            phases += [dnnlib.EasyDict(name=name+'main', module=module, opt=opt, interval=1)]
            phases += [dnnlib.EasyDict(name=name+'reg', module=module, opt=opt, interval=reg_interval)]
            

    for name, module, opt_kwargs, reg_interval in [('our_encoder', our_encoder, our_encoder_opt_kwargs, None)]:
        opt = dnnlib.util.construct_class_by_name(params=module.parameters(), **opt_kwargs)
        phases += [dnnlib.EasyDict(name='our_encoder', module=module, opt=opt, interval=1)]
    
    
    for phase in phases:
        phase.start_event = None
        phase.end_event = None
        if rank == 0:
            phase.start_event = torch.cuda.Event(enable_timing=True)
            phase.end_event = torch.cuda.Event(enable_timing=True)

    """ # Export sample images.
    grid_size = None
    grid_z = None
    grid_c = None
    if rank == 0:
        print('Exporting sample images...')
        grid_size, images, labels = setup_snapshot_image_grid(training_set=training_set)
        save_image_grid(images, os.path.join(run_dir, 'reals.png'), drange=[0,255], grid_size=grid_size)
        grid_z = torch.randn([labels.shape[0], G.z_dim], device=device).split(batch_gpu)
        grid_c = torch.from_numpy(labels).to(device).split(batch_gpu)
        images = torch.cat([G_ema(z=z, c=c, noise_mode='const').cpu() for z, c in zip(grid_z, grid_c)]).numpy()
        save_image_grid(images, os.path.join(run_dir, 'fakes_init.png'), drange=[-1,1], grid_size=grid_size) """

    """ # Initialize logs.
    if rank == 0:
        print('Initializing logs...')
    stats_collector = training_stats.Collector(regex='.*')
    stats_metrics = dict()
    stats_jsonl = None
    stats_tfevents = None
    if rank == 0:
        stats_jsonl = open(os.path.join(run_dir, 'stats.jsonl'), 'wt')
        try:
            import torch.utils.tensorboard as tensorboard
            stats_tfevents = tensorboard.SummaryWriter(run_dir)
        except ImportError as err:
            print('Skipping tfevents export:', err) """

    """ # Train.
    if rank == 0:
        print(f'Training for {total_kimg} kimg...')
        print() """
    
    #cur_tick = 0
    #tick_start_nimg = cur_nimg
    #tick_start_time = time.time()
    #maintenance_time = tick_start_time - start_time

    batch_idx = 0
    if progress_fn is not None:
        progress_fn(0, total_kimg)

    
    # test samples for evaluation
    smp_idices = np.random.choice(len(test_set), n_source_imgs, replace=False)
    eval_output = run_dir
    if not os.path.exists(eval_output):
        os.makedirs(eval_output)
    eval_encoder(our_encoder, test_set, n_source_imgs, img_res, device, G, eval_output, smp_idices, enc_arch)

        


#----------------------------------------------------------------------------
