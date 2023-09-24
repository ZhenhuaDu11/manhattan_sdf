from lib.config import cfg, args
from lib.networks import make_network
from lib.train import make_trainer, make_optimizer, make_lr_scheduler, make_recorder, set_lr_scheduler
from lib.datasets import make_data_loader
from lib.utils.net_utils import load_model, save_model, load_network, load_pretrain
from lib.evaluators import make_evaluator
import torch.multiprocessing
import torch
import torch.distributed as dist
import os
import numpy as np
import random
torch.autograd.set_detect_anomaly(True)
import numpy
if cfg.fix_random:
    torch.manual_seed(0)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False

def train(cfg, network):
    trainer = make_trainer(cfg, network)
    optimizer = make_optimizer(cfg, network)
    scheduler = make_lr_scheduler(cfg, optimizer)
    recorder = make_recorder(cfg)

    begin_epoch = load_model(
        network,
        optimizer,
        scheduler,
        recorder,
        cfg.trained_model_dir,
        resume=cfg.resume
    )
    
    if begin_epoch == 0 and cfg.pretrain != '':
        load_pretrain(network, cfg.pretrain, cfg.trained_model_dir)
        
    set_lr_scheduler(cfg, scheduler)

    train_loader = make_data_loader(
        cfg,
        is_train=True,
        is_distributed=cfg.distributed,
        max_iter=cfg.ep_iter
    )
    
    test_loader = make_data_loader(cfg, is_train=False)
    begin_epoch=0                              
    for epoch in range(begin_epoch, cfg.train.epoch):
        recorder.epoch = epoch
        if cfg.distributed:
            train_loader.batch_sampler.sampler.set_epoch(epoch)

        trainer.train(epoch, train_loader, optimizer, recorder)
        scheduler.step()

        if (epoch + 1) % cfg.save_ep == 0 and cfg.local_rank == 0:
            save_model(network, optimizer, scheduler, recorder, cfg.trained_model_dir, epoch)

        if (epoch + 1) % cfg.save_latest_ep == 0 and cfg.local_rank == 0:
            save_model(
                network,
                optimizer,
                scheduler,
                recorder,
                cfg.trained_model_dir,
                epoch,
                last=True
            )

        if (epoch + 1) % cfg.eval_ep == 0 and cfg.local_rank == 0:
            trainer.val(epoch, data_loader=test_loader)
    
    trainer.render(epoch, data_loader=test_loader)
    return network

def test(cfg, network):
    trainer = make_trainer(cfg, network)
    test_loader = make_data_loader(cfg, is_train=False)
    evaluator = make_evaluator(cfg)

    epoch = load_network(
        network,
        cfg.trained_model_dir,
        resume=cfg.resume,
        epoch=cfg.test.epoch
    )
    trainer.render(epoch, data_loader=test_loader)
    # trainer.val(epoch, data_loader=test_loader, evaluate_mesh=True, evaluator=evaluator)


def synchronize():
    """
    Helper function to synchronize (barrier) among all processes when
    using distributed training
    """
    if not dist.is_available():
        return
    if not dist.is_initialized():
        return
    world_size = dist.get_world_size()
    if world_size == 1:
        return
    dist.barrier()


def main():
    if cfg.distributed:
        cfg.local_rank = int(os.environ['RANK']) % torch.cuda.device_count()
        torch.cuda.set_device(cfg.local_rank)
        torch.distributed.init_process_group(backend="nccl", init_method="env://")
        synchronize()

    network = make_network(cfg)
    if args.test:
        test(cfg, network)
    else:
        train(cfg, network)


if __name__ == "__main__":
    main()