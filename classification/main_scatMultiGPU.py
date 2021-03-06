import argparse
import os
import shutil
import time
import numpy as np
import json
import datetime
from random import randint
import torch
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.optim
import torch.utils.data
import torchvision.transforms as transforms
import torchvision.datasets as datasets
import models
from pyscatlight import Scatlight as Scattering

import torch.cuda.comm as comm

#### ADDED by Edouard

from torch.nn.parallel import scatter, parallel_apply, gather
from multi_scat import DataParallelScat


class ScatteringMultiGPU:
    def __init__(self, M, N, J, device_ids):
        self.modules = []
        for dev in device_ids:
            with torch.cuda.device(dev):
                self.modules.append(Scattering(M, N, J).cuda())
        self.device_ids = device_ids

    def forward(self, inputs):
        inputs_multi = comm.scatter(inputs, self.device_ids)
        tensors = parallel_apply(self.modules, [(v,) for v in inputs_multi], devices=self.device_ids)
        out=[]

        for i, tensor in enumerate(tensors):
            with torch.cuda.device(tensor.get_device()):
                tensors[i] = torch.autograd.Variable(tensors[i])
                out.append([tensors[i]])

        
        return out

    def __call__(self, inputs):
        return self.forward(inputs)



model_names = sorted(name for name in models.__dict__
                     if name.islower() and not name.startswith("__")
                     and callable(models.__dict__[name]))

parser = argparse.ArgumentParser(description='PyTorch ImageNet Training')
parser.add_argument('data', metavar='DIR',
                    help='path to dataset')
#### MODIFIED by Edouard
parser.add_argument('--arch', '-a', metavar='ARCH', default='scat_resnet_big',
                    choices=model_names,
                    help='model architecture: ' +
                         ' | '.join(model_names) +
                         ' (default: scat_resnet_big)')
parser.add_argument('-j', '--workers', default=4, type=int, metavar='N',
                    help='number of data loading workers (default: 4)')
parser.add_argument('--epochs', default=90, type=int, metavar='N',
                    help='number of total epochs to run')
parser.add_argument('--start-epoch', default=0, type=int, metavar='N',
                    help='manual epoch number (useful on restarts)')
parser.add_argument('-b', '--batch-size', default=256, type=int,
                    metavar='N', help='mini-batch size (default: 256)')
parser.add_argument('--lr', '--learning-rate', default=0.1, type=float,
                    metavar='LR', help='initial learning rate')
parser.add_argument('--momentum', default=0.9, type=float, metavar='M',
                    help='momentum')
parser.add_argument('--weight-decay', '--wd', default=1e-4, type=float,
                    metavar='W', help='weight decay (default: 1e-4)')
parser.add_argument('--print-freq', '-p', default=10, type=int,
                    metavar='N', help='print frequency (default: 10)')
parser.add_argument('--resume', default='', type=str, metavar='PATH',
                    help='path to latest checkpoint (default: none)')
parser.add_argument('-e', '--evaluate', dest='evaluate', action='store_true',
                    help='evaluate model on validation set')
parser.add_argument('--pretrained', dest='pretrained', action='store_true',
                    help='use pre-trained model')

#### ADDED by Edouard
parser.add_argument('--J', '--scale_scattering', default=3, type=int, metavar='N',
                    help='scale to select to compute the order 1 scattering')
parser.add_argument('--save_folder', default='', type=str, help='where to save the models')
parser.add_argument('--bottleneck_width', default='[128,256]', type=str, help='size of the bottleneck')
parser.add_argument('--bottleneck_depth', default='[3,3]', type=str, help='size of the bottleneck')
parser.add_argument('--bottleneck_conv1x1', default=0, type=int, help='number of 1x1')
best_prec1 = 0


#### ADDED by Edouard

def main():
    global args, folder_save
    args = parser.parse_args()

    print(args)

    opts = vars(args)

    name_log = ''.join('{}{}-'.format(key, val) for key, val in sorted(opts.items()) if key is not 'rank')
    name_log = name_log.replace('/', '-')
    name_log = name_log.replace('[', '-')
    name_log = name_log.replace(']', '-')

    name_log_list = list(map(''.join, zip(*[iter(name_log)] * 100)))
    # name_log = '/'.join(name_log_list)

    print(name_log_list, '\n')

    folder_save = args.save_folder
    time_stamp = str(datetime.datetime.now().isoformat())
    log_file = time_stamp + str(randint(0, 1000)) + '.log'
    name_log_txt = folder_save + '/'+log_file


    for i in range(len(name_log_list)):
        folder_save = os.path.join(folder_save, name_log_list[i])

        if not os.path.isdir(folder_save):
            os.mkdir(folder_save)


    print('This will be saved in: ' + folder_save, '\n')

    args.bottleneck_width = json.loads(args.bottleneck_width)
    args.bottleneck_depth = json.loads(args.bottleneck_depth)


    global best_prec1
    global scat


    def save_checkpoint(state, is_best, filename=os.path.join(folder_save, 'checkpoint.pth.tar')):
        torch.save(state, filename)
        if is_best:
            shutil.copyfile(filename, os.path.join(folder_save, 'model_best.pth.tar'))


    # create model
    model = models.__dict__[args.arch](224, args.J,
                                             width = args.bottleneck_width,
                                             depth= args.bottleneck_depth,
                                             conv1x1=args.bottleneck_conv1x1)
    with open(log_file, "a") as text_file:
        print(model, file=text_file)

    scat = ScatteringMultiGPU(224, 224, 3, list(range(torch.cuda.device_count())))


    model = DataParallelScat(model).cuda()

    model_parameters = filter(lambda p: p.requires_grad, model.parameters())
    params = sum([np.prod(p.size()) for p in model_parameters])

    print('Number of parameters: %d' % params)
    #### MODIFIED by Edouard

    save_checkpoint({
        'epoch': -1,
        'arch': args.arch,
        'state_dict': model.state_dict(),
        'best_prec1': 0,
    }, False)



    # define loss function (criterion) and optimizer
    criterion = nn.CrossEntropyLoss().cuda()

    optimizer = torch.optim.SGD(model.parameters(), args.lr,
                                momentum=args.momentum,
                                weight_decay=args.weight_decay)

    # optionally resume from a checkpoint
    if args.resume:
        if os.path.isfile(args.resume):
            print("=> loading checkpoint '{}'".format(args.resume))

            checkpoint = torch.load(args.resume, map_location=lambda storage, loc: storage.cuda(args.gpu))
            args.start_epoch = checkpoint['epoch']
            best_prec1 = checkpoint['best_prec1']
            model.load_state_dict(checkpoint['state_dict'])
            optimizer.load_state_dict(checkpoint['optimizer'])
            print("=> loaded checkpoint '{}' (epoch {})"
                  .format(args.resume, checkpoint['epoch']))
        else:
            print("=> no checkpoint found at '{}'".format(args.resume))

    cudnn.benchmark = True

    # Data loading code
    traindir = os.path.join(args.data, 'train')
    valdir = os.path.join(args.data, 'val')
    normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                     std=[0.229, 0.224, 0.225])

    train_dataset = datasets.ImageFolder(
        traindir,
        transforms.Compose([
            transforms.RandomResizedCrop(224),
            transforms.RandomHorizontalFlip(),
            transforms.ToTensor(),
            normalize,
        ]))

    train_sampler = None
    train_loader = torch.utils.data.DataLoader(
        train_dataset, batch_size=args.batch_size, shuffle=True,
        num_workers=args.workers, pin_memory=True)

    val_loader = torch.utils.data.DataLoader(
        datasets.ImageFolder(valdir, transforms.Compose([
            transforms.Resize(256),
            transforms.CenterCrop(224),
            transforms.ToTensor(),
            normalize,
        ])),
        batch_size=args.batch_size, shuffle=False,
        num_workers=args.workers, pin_memory=True)

    if args.evaluate:
        validate(val_loader, model, criterion)
        return

    for epoch in range(args.start_epoch, args.epochs):
        adjust_learning_rate(optimizer, epoch)

        # train for one epoch
        top1train,top5train = train(train_loader, model, criterion, optimizer, epoch)

        # evaluate on validation set
        prec1,prec5 = validate(val_loader, model, criterion)

        # remember best prec@1 and save checkpoint
        is_best = prec1 > best_prec1
        best_prec1 = max(prec1, best_prec1)
        save_checkpoint({
            'epoch': epoch + 1,
            'arch': args.arch,
            'state_dict': model.state_dict(),
            'best_prec1': best_prec1,
            'optimizer': optimizer.state_dict(),
        }, is_best)
        with open(log_file, "a") as text_file:
            print(" epoch {}, train top1:{}(top5:{}), test top1:{} (top5:{})"
                  .format(epoch, top1train, top5train, prec1, prec5), file=text_file)


def train(train_loader, model, criterion, optimizer, epoch):
    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()
    top5 = AverageMeter()

    # switch to train mode
    model.train()

    end = time.time()
    for i, (input, target) in enumerate(train_loader):
        # measure data loading time
        data_time.update(time.time() - end)
        s = input.size(0)
        #### FIXED BY Eugene
        target = target.cuda(async=True)
        input = input.cuda()
        now = time.time()
        input = scat(input)
        scat_time = time.time() - now
        input_var = input
        target_var = torch.autograd.Variable(target)

        # compute output
        output = model(*input_var)
        loss = criterion(output, target_var)

        # measure accuracy and record loss
        prec1, prec5 = accuracy(output.data, target, topk=(1, 5))
        losses.update(loss.data[0], s)
        top1.update(prec1[0], s)
        top5.update(prec5[0], s)

        # compute gradient and do SGD step
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        if i % args.print_freq == 0:
            print('Epoch: [{0}][{1}/{2}]\t'
                  'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                  'Data {data_time.val:.3f} ({data_time.avg:.3f})\t'
                  'Scat {scat_time:.3f} \t'
                  'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                  'Prec@1 {top1.val:.3f} ({top1.avg:.3f})\t'
                  'Prec@5 {top5.val:.3f} ({top5.avg:.3f})'.format(
                epoch, i, len(train_loader), batch_time=batch_time,
                data_time=data_time, scat_time=scat_time, loss=losses, top1=top1, top5=top5))
    return top1.avg,top5.avg

def validate(val_loader, model, criterion):
    batch_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()
    top5 = AverageMeter()

    # switch to evaluate mode
    model.eval()

    end = time.time()
    for i, (input, target) in enumerate(val_loader):
        target = target.cuda(async=True)
        s  = input.size(0)
        #### MODIFIED by Edouard
        input = scat(input.cuda())

        input_var = input#torch.autograd.Variable(input, volatile=True)
        target_var = torch.autograd.Variable(target, volatile=True)

        # compute output
        output = model(*input_var)
        loss = criterion(output, target_var)

        # measure accuracy and record loss
        prec1, prec5 = accuracy(output.data, target, topk=(1, 5))
        losses.update(loss.data[0], s)
        top1.update(prec1[0], s)
        top5.update(prec5[0], s)

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        if i % args.print_freq == 0:
            print('Test: [{0}/{1}]\t'
                  'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                  'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                  'Prec@1 {top1.val:.3f} ({top1.avg:.3f})\t'
                  'Prec@5 {top5.val:.3f} ({top5.avg:.3f})'.format(
                i, len(val_loader), batch_time=batch_time, loss=losses,
                top1=top1, top5=top5))

    print(' * Prec@1 {top1.avg:.3f} Prec@5 {top5.avg:.3f}'
          .format(top1=top1, top5=top5))

    return top1.avg,top5.avg


class AverageMeter(object):
    """Computes and stores the average and current value"""

    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


def adjust_learning_rate(optimizer, epoch):
    """Sets the learning rate to the initial LR decayed by 10 every 30 epochs"""
    lr = args.lr * (0.1 ** (epoch // 30))
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr


def accuracy(output, target, topk=(1,)):
    """Computes the precision@k for the specified values of k"""
    maxk = max(topk)
    batch_size = target.size(0)

    _, pred = output.topk(maxk, 1, True, True)
    pred = pred.t()
    correct = pred.eq(target.view(1, -1).expand_as(pred))

    res = []
    for k in topk:
        correct_k = correct[:k].view(-1).float().sum(0, keepdim=True)
        res.append(correct_k.mul_(100.0 / batch_size))
    return res


if __name__ == '__main__':
    main()
