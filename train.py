import os.path
import shutil
from config import get_config
from scheduler import MipLRDecay
from loss import NeRFLoss, mse_to_psnr
from model import MipNeRF
import torch
import torch.optim as optim
import torch.utils.tensorboard as tb
from os import path
from datasets import get_dataloader, cycle
import numpy as np
from tqdm import tqdm
import imageio

def train_model(config):
    model_save_path = path.join(config.log_dir, "model.pt")
    # optimizer_save_path = path.join(config.log_dir, "optim.pt")

    data = iter(cycle(get_dataloader(dataset_name=config.dataset_name, base_dir=config.base_dir, split="train", factor=config.factor, batch_size=config.batch_size, shuffle=True, device=config.device)))
    eval_data = None
    if config.do_eval:
        eval_data = iter(cycle(get_dataloader(dataset_name=config.dataset_name, base_dir=config.base_dir, split="test", factor=config.factor, batch_size=config.batch_size, shuffle=True, device=config.device)))
    
    render_data = get_dataloader(config.dataset_name, config.base_dir, split="render", factor=config.factor, shuffle=False, n_poses=config.n_poses, h=200, w=200)

    model = MipNeRF(
        use_viewdirs=config.use_viewdirs,
        randomized=config.randomized,
        ray_shape=config.ray_shape,
        white_bkgd=config.white_bkgd,
        num_levels=config.num_levels,
        num_samples=config.num_samples,
        hidden=config.hidden,
        density_noise=config.density_noise,
        density_bias=config.density_bias,
        rgb_padding=config.rgb_padding,
        resample_padding=config.resample_padding,
        min_deg=config.min_deg,
        max_deg=config.max_deg,
        viewdirs_min_deg=config.viewdirs_min_deg,
        viewdirs_max_deg=config.viewdirs_max_deg,
        device=config.device,
    )
    optimizer = optim.AdamW(model.parameters(), lr=config.lr_init, weight_decay=config.weight_decay)

    start_step = 0
    if config.continue_training:
        model_info = torch.load(model_save_path)
        model.load_state_dict(model_info['state_dict'])
        optimizer.load_state_dict(model_info['optimizer'])
        start_step = model_info['step']
        print("Loaded model and optimizer from disk.")

    scheduler = MipLRDecay(optimizer, lr_init=config.lr_init, lr_final=config.lr_final, max_steps=config.max_steps, lr_delay_steps=config.lr_delay_steps, lr_delay_mult=config.lr_delay_mult)
    loss_func = NeRFLoss(config.coarse_weight_decay)
    model.train()

    os.makedirs(config.log_dir, exist_ok=True)
    shutil.rmtree(path.join(config.log_dir, 'train'), ignore_errors=True)
    logger = tb.SummaryWriter(path.join(config.log_dir, 'train'), flush_secs=1)

    for step in tqdm(range(start_step, config.max_steps)):
        model.train()
        rays, pixels = next(data)
        comp_rgb, _, _ = model(rays)
        pixels = pixels.to(config.device)

        # Compute loss and update model weights.
        loss_val, psnr = loss_func(comp_rgb, pixels, rays.lossmult.to(config.device))
        optimizer.zero_grad()
        loss_val.backward()
        optimizer.step()
        scheduler.step()

        psnr = psnr.detach().cpu().numpy()
        logger.add_scalar('train/loss', float(loss_val.detach().cpu().numpy()), global_step=step)
        logger.add_scalar('train/coarse_psnr', float(np.mean(psnr[:-1])), global_step=step)
        logger.add_scalar('train/fine_psnr', float(psnr[-1]), global_step=step)
        logger.add_scalar('train/avg_psnr', float(np.mean(psnr)), global_step=step)
        logger.add_scalar('train/lr', float(scheduler.get_last_lr()[-1]), global_step=step)

        if step % config.save_every == 0:
            save_model(model, optimizer, step, model_save_path=os.path.join(config.log_dir, f"model_{step}.pt"))
            save_model(model, optimizer, step, model_save_path=model_save_path)
            if eval_data:
                del rays
                del pixels
                psnr = eval_model(config, model, eval_data)
                psnr = psnr.detach().cpu().numpy()
                logger.add_scalar('eval/coarse_psnr', float(np.mean(psnr[:-1])), global_step=step)
                logger.add_scalar('eval/fine_psnr', float(psnr[-1]), global_step=step)
                logger.add_scalar('eval/avg_psnr', float(np.mean(psnr)), global_step=step)

        if step % config.render_every == 0:
            # render model
            model.eval()
            print("Generating Video using", len(render_data), "different view points")
            rgb_frames = []
            for rays in tqdm(render_data):
                img, _, _ = model.render_image(rays, render_data.h, render_data.w, chunks=config.chunks)
                rgb_frames.append(img)
            os.makedirs(path.join(config.log_dir, f"step_{step}"), exist_ok=True)
            img_cnt = 0
            for img in rgb_frames:
                imageio.imwrite(path.join(config.log_dir, f"step_{step}/image_{img_cnt}.png"), img)
                img_cnt += 1
            imageio.mimwrite(path.join(config.log_dir, f"step_{step}/video.mp4"), rgb_frames, fps=30, quality=10)
            
    # save_model(model, optimizer, step, model_save_path)
    # torch.save(model.state_dict(), model_save_path)
    # torch.save(optimizer.state_dict(), optimizer_save_path)

def save_model(model, optimizer, step, model_save_path):
    model_info = {'state_dict': model.state_dict(), 'optimizer': optimizer.state_dict(), 'step': step}
    torch.save(model_info, model_save_path)

def eval_model(config, model, data):
    model.eval()
    rays, pixels = next(data)
    with torch.no_grad():
        comp_rgb, _, _ = model(rays)
    pixels = pixels.to(config.device)
    model.train()
    return torch.tensor([mse_to_psnr(torch.mean((rgb - pixels[..., :3])**2)) for rgb in comp_rgb])


if __name__ == "__main__":
    config = get_config()
    train_model(config)
