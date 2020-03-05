#!/usr/bin/env python
# coding: utf-8

from __future__ import print_function

import copy
import os.path as osp
from importlib import import_module
import matplotlib.pyplot as plt
import click
import cv2
import matplotlib.cm as cm
import numpy as np
import torch
import glob as gb
#import torch.hub
import torch.nn.functional as F
from torch.autograd import Variable
from torchvision import models, transforms

from grad_cam import (
    BackPropagation,
    Deconvnet,
    GradCAM,
    GuidedBackPropagation,
    occlusion_sensitivity,
)

# if a model includes LSTM, such as in image captioning,
# torch.backends.cudnn.enabled = False

def draw_features(width, height, x, output_dir, target_layer):
    #tic=time.time()
    fig = plt.figure(figsize=(16, 16))
    fig.subplots_adjust(left=0.05, right=0.95, bottom=0.05, top=0.95, wspace=0.05, hspace=0.05)
    for i in range(x.shape[0]):
        for j in range(width*height):
            plt.subplot(height, width, j + 1)
            plt.axis('off')
            img = x[i, j, :, :]
            pmin = np.min(img)
            pmax = np.max(img)
            img = (img - pmin) / (pmax - pmin + 0.000001)
            imgplot = plt.imshow(img)
            plt.colorbar()
            #plt.imshow(img, cmap='hot')
            print("{}/{}".format(j,width*height))
        savename = "{}/{}-channelmap-{}.png".format(output_dir, i, target_layer)    
        fig.savefig(savename, dpi=100)
        fig.clf()
    plt.close()
    #print("time:{}".format(time.time()-tic))  

def get_device(cuda):
    cuda = cuda and torch.cuda.is_available()
    device = torch.device("cuda" if cuda else "cpu")
    if cuda:
        current_device = torch.cuda.current_device()
        print("Device:", torch.cuda.get_device_name(current_device))
    else:
        print("Device: CPU")
    return device


def get_classtable():
    classes = []
    with open("samples/synset_words.txt") as lines:
        for line in lines:
            line = line.strip().split(" ", 1)[1]
            line = line.split(", ", 1)[0].replace(" ", "_")
            classes.append(line)
    return classes


def preprocess(image_path):
    raw_image = cv2.imread(image_path)
    raw_image = cv2.resize(raw_image, (128, 384))
    image = transforms.Compose(
        [
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
        ]
    )(raw_image[..., ::-1].copy())
    return image, raw_image


def save_gradient(filename, gradient):
    gradient = gradient.cpu().numpy().transpose(1, 2, 0)
    gradient -= gradient.min()
    gradient /= gradient.max()
    gradient *= 255.0
    cv2.imwrite(filename, np.uint8(gradient))


def save_gradcam(filename, gcam, raw_image, paper_cmap=False):
    gcam = gcam.cpu().numpy()
    cmap = cm.jet_r(gcam)[..., :3] * 255.0
    if paper_cmap:
        alpha = gcam[..., None]
        gcam = alpha * cmap + (1 - alpha) * raw_image
    else:
        gcam = (cmap.astype(np.float) + raw_image.astype(np.float)) / 2
    cv2.imwrite(filename, np.uint8(gcam))


def save_sensitivity(filename, maps):
    maps = maps.cpu().numpy()
    scale = max(maps[maps > 0].max(), -maps[maps <= 0].min())
    maps = maps / scale * 0.5
    maps += 0.5
    maps = cm.bwr_r(maps)[..., :3]
    maps = np.uint8(maps * 255.0)
    maps = cv2.resize(maps, (128, 384), interpolation=cv2.INTER_NEAREST)
    cv2.imwrite(filename, maps)


# torchvision models
model_names = sorted(
    name
    for name in models.__dict__
    if name.islower() and not name.startswith("__") and callable(models.__dict__[name])
)


@click.group()
@click.pass_context
def main(ctx):
    print("Mode:", ctx.invoked_subcommand)


@main.command()
@click.option("-i", "--image-paths", type=str, multiple=True, required=True)
@click.option("-n", "--model-name", type=str, required=True)
@click.option("-p", "--model-path", type=str, required=True)
@click.option("-a", "--arch", type=click.Choice(model_names), required=True)
@click.option("-t", "--target-layer", type=str, required=True)
@click.option("-k", "--topk", type=int, default=3)
@click.option("-o", "--output-dir", type=str, default="./results")
@click.option("--cuda/--cpu", default=True)
def visualization(image_paths, model_name, model_path, target_layer, arch, topk, output_dir, cuda):
    """
    Visualize model responses given multiple images
    """

    device = get_device(cuda)

    # Synset words
    classes = get_classtable()

    # Model 
    kwargs = {}
    module = import_module(model_name) 
    model = module.make_model().to(device)
    model.load_state_dict(
                        torch.load(model_path, **kwargs),
                        strict=False
                    )
    #print(model)
    model.to(device)
    model.eval()
    
    # Images
    images = []
    raw_images = []
    print("Images:")
    
    path = gb.glob(image_paths[0] + '/*.jpg')
    index = 0 
    for img in path:
        print("\t#{}: {}".format(index, img))
        image, raw_image = preprocess(img)
        images.append(image)
        raw_images.append(raw_image)
        index = index + 1
    images = torch.stack(images).to(device)

    """
    Common usage:
    1. Wrap your model with visualization classes defined in grad_cam.py
    2. Run forward() with images
    3. Run backward() with a list of specific classes
    4. Run generate() to export results
    """

    # =========================================================================
    print("Vanilla Backpropagation:")

    bp = BackPropagation(model=model)
    probs, ids = bp.forward(images)
    #print(probs, ids)
    for i in range(topk):
        # In this example, we specify the high confidence classes
        bp.backward(ids=ids[:, [i]])
        gradients = bp.generate()

        # Save results as image files
        for j in range(len(images)):
            print("\t#{}: {} ({:.5f})".format(j, ids[j, i], probs[j, i]))

            save_gradient(
                filename=osp.join(
                    output_dir,
                    #"{}-{}-vanilla-{}.png".format(j, arch, classes[ids[j, i]]),
                    "{}-{}-vanilla-{}.png".format(j, arch, ids[j, i]),
                ),
                gradient=gradients[j],
            )

    # Remove all the hook function in the "model"
    bp.remove_hook()

    # =========================================================================
    print("Deconvolution:")

    deconv = Deconvnet(model=model)
    _ = deconv.forward(images)

    for i in range(topk):
        deconv.backward(ids=ids[:, [i]])
        gradients = deconv.generate()

        for j in range(len(images)):
            print("\t#{}: {} ({:.5f})".format(j, ids[j, i], probs[j, i]))

            save_gradient(
                filename=osp.join(
                    output_dir,
                    #"{}-{}-deconvnet-{}.png".format(j, arch, classes[ids[j, i]]),
                    "{}-{}-deconvnet-{}.png".format(j, arch, ids[j, i]),
                ),
                gradient=gradients[j],
            )

    deconv.remove_hook()

    # =========================================================================
    print("Grad-CAM/Guided Backpropagation/Guided Grad-CAM:")

    gcam = GradCAM(model=model)
    _ = gcam.forward(images)

    gbp = GuidedBackPropagation(model=model)
    _ = gbp.forward(images)

    for i in range(topk):
        # Guided Backpropagation
        gbp.backward(ids=ids[:, [i]])
        #print(ids[:, [i]])
        gradients = gbp.generate()

        # Grad-CAM
        gcam.backward(ids=ids[:, [i]])
        regions = gcam.generate(target_layer=target_layer)
        #print(regions)

        for j in range(len(images)):
            print("\t#{}: {} ({:.5f})".format(j, ids[j, i], probs[j, i]))
            
            # Guided Backpropagation
            save_gradient(
                filename=osp.join(
                    output_dir,
                    #"{}-{}-guided-{}.png".format(j, arch, classes[ids[j, i]]),
                    "{}-{}-guided-{}.png".format(j, arch, ids[j, i]),
                ),
                gradient=gradients[j],
            )

            # Grad-CAM
            save_gradcam(
                filename=osp.join(
                    output_dir,
                    "{}-{}-gradcam-{}-{}.png".format(
                        #j, arch, target_layer, classes[ids[j, i]]
                        j, arch, target_layer, ids[j, i]
                    ),
                ),
                gcam=regions[j, 0],
                raw_image=raw_images[j],
            )
            #print(regions.shape)
            # Guided Grad-CAM
            save_gradient(
                filename=osp.join(
                    output_dir,
                    "{}-{}-guided_gradcam-{}-{}.png".format(
                        #j, arch, target_layer, classes[ids[j, i]]
                        j, arch, target_layer, ids[j, i]
                    ),
                ),
                gradient=torch.mul(regions, gradients)[j],
            )
    # =========================================================================
    print("Channel Visialization:")
    gcam = GradCAM(model=model)
    _ = gcam.forward(images)
    feature_map = gcam.channel_visualization(target_layer=target_layer)
    draw_features(4, 4, feature_map, output_dir, target_layer)
        

if __name__ == "__main__":
    main()
