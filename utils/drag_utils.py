# *************************************************************************
# Copyright (2023) Bytedance Inc.
#
# Copyright (2023) DragDiffusion Authors 
#
# Licensed under the Apache License, Version 2.0 (the "License"); 
# you may not use this file except in compliance with the License. 
# You may obtain a copy of the License at 
#
#     http://www.apache.org/licenses/LICENSE-2.0 
#
# Unless required by applicable law or agreed to in writing, software 
# distributed under the License is distributed on an "AS IS" BASIS, 
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. 
# See the License for the specific language governing permissions and 
# limitations under the License. 
# *************************************************************************



import copy
import torch
import torch.nn.functional as F
import numpy as np 
from PIL import Image
import clip
import torchvision
from utils.augmentations import ImageAugmentations
from torchvision import transforms
import os



def fast_point_tracking(F0,
                   F1,
                   handle_points,
                   handle_points_init,
                   target_points,
                   args):
    # point tracking a: consider distance to target.
    with torch.no_grad():
        _, _, max_r, max_c = F0.shape
        for i in range(len(handle_points)):
            pi0, pi = handle_points_init[i], handle_points[i]
            f0 = F0[:, :, int(pi0[0]), int(pi0[1])]

            r1, r2 = max(0,int(pi[0])-args.r_p), min(max_r,int(pi[0])+args.r_p+1)
            c1, c2 = max(0,int(pi[1])-args.r_p), min(max_c,int(pi[1])+args.r_p+1)
  
            F1_neighbor = F1[:, :, r1:r2, c1:c2]
            all_dist = (f0.unsqueeze(dim=-1).unsqueeze(dim=-1) - F1_neighbor).abs().sum(dim=1)
            all_dist = all_dist.squeeze(dim=0)
            ################################################################
            x,y  = torch.range(r1,r2-1),torch.range(c1,c2-1)
            xx,yy = torch.meshgrid(x,y)
            points = torch.stack([xx.flatten(),yy.flatten()],dim=1)
            point_dist = torch.sum((points-target_points[i])**2,dim=1).reshape(r2-r1,c2-c1)
            threshold = torch.sum((handle_points[i]-target_points[i])**2)
            all_dist[point_dist>=threshold] = float('inf')
            if threshold <=1:
                all_dist[args.r_p,args.r_p] = threshold 
            ################################################################
            row, col = divmod(all_dist.argmin().item(), all_dist.shape[-1])
            # handle_points[i][0] = pi[0] - args.r_p + row
            # handle_points[i][1] = pi[1] - args.r_p + col
            handle_points[i][0] = r1 + row
            handle_points[i][1] = c1 + col
            dist = torch.sum((handle_points[i]-target_points[i])**2)
            print(f'{i}-th point pairs: handle_point:{handle_points[i]},target_point:{target_points[i]},dist:{dist}')

        return handle_points

def check_handle_reach_target(handle_points,
                              target_points):
    # dist = (torch.cat(handle_points,dim=0) - torch.cat(target_points,dim=0)).norm(dim=-1)
    all_dist = list(map(lambda p,q: (p-q).norm(), handle_points, target_points))
    return (torch.tensor(all_dist) < 2.0).all()

# obtain the bilinear interpolated feature patch centered around (x, y) with radius r
def interpolate_feature_patch(feat,
                              y1,
                              y2,
                              x1,
                              x2):
    x1_floor = torch.floor(x1).long()
    x1_cell = x1_floor + 1
    dx = torch.floor(x2).long() - torch.floor(x1).long()

    y1_floor = torch.floor(y1).long()
    y1_cell = y1_floor + 1
    dy = torch.floor(y2).long() - torch.floor(y1).long()

    wa = (x1_cell.float() - x1) * (y1_cell.float() - y1)
    wb = (x1_cell.float() - x1) * (y1 - y1_floor.float())
    wc = (x1 - x1_floor.float()) * (y1_cell.float() - y1)
    wd = (x1 - x1_floor.float()) * (y1 - y1_floor.float())

    Ia = feat[:, :, y1_floor : y1_floor+dy, x1_floor : x1_floor+dx]
    Ib = feat[:, :, y1_cell : y1_cell+dy, x1_floor : x1_floor+dx]
    Ic = feat[:, :, y1_floor : y1_floor+dy, x1_cell : x1_cell+dx]
    Id = feat[:, :, y1_cell : y1_cell+dy, x1_cell : x1_cell+dx]

    return Ia * wa + Ib * wb + Ic * wc + Id * wd


def drag_diffusion_update(model,
                          init_code,
                          text_embeddings,
                          t,
                          handle_points,
                          target_points,
                          mask,
                          args):

    assert len(handle_points) == len(target_points), \
        "number of handle point must equals target points"
    if text_embeddings is None:
        text_embeddings = model.get_text_embeddings(args.prompt)
    for param in model.unet.parameters():
        param.requires_grad = False
    for param in model.vae.parameters():
        param.requires_grad = False
    device = "cuda" if torch.cuda.is_available() else "cpu"
    clip_model = clip.load("ViT-B/16", device=device)[0].eval().requires_grad_(False)
    # the init output feature of unet
    with torch.no_grad():
        unet_output, F0 = model.forward_unet_features(init_code, t,
            encoder_hidden_states=text_embeddings,
            layer_idx=args.unet_feature_idx, interp_res_h=args.sup_res_h, interp_res_w=args.sup_res_w)
        x_prev_0,_ = model.step(unet_output, t, init_code)
        # init_code_orig = copy.deepcopy(init_code)

    # prepare optimizable init_code and optimizer
    init_code.requires_grad_(True)
    optimizer = torch.optim.Adam([init_code], lr=args.lr)

    # prepare for point tracking and background regularization
    handle_points_init = copy.deepcopy(handle_points)
    interp_mask = F.interpolate(mask, (init_code.shape[2],init_code.shape[3]), mode='nearest')
    using_mask = interp_mask.sum() != 0.0
    # prepare amp scaler for mixed-precision training
    scaler = torch.cuda.amp.GradScaler()
    for step_idx in range(args.n_pix_step):
        print(f'iteration index:{step_idx}')
        with torch.autocast(device_type='cuda', dtype=torch.float16):
            ###################################### clip guidance part ###################
            unet_output, F1 = model.forward_unet_features(init_code, t,
                encoder_hidden_states=text_embeddings,
                layer_idx=args.unet_feature_idx, interp_res_h=args.sup_res_h, interp_res_w=args.sup_res_w)
            x_prev_updated,_ = model.step(unet_output, t, init_code)

            # do point tracking to update handle points before computing motion supervision loss
            if step_idx != 0:
                handle_points = fast_point_tracking(F0, F1, handle_points, handle_points_init,target_points, args)
                # handle_points = point_tracking(F0, F1, handle_points, handle_points_init, args)
                print('new handle points', handle_points)

            # break if all handle points have reached the targets
            if check_handle_reach_target(handle_points, target_points):
                break

            loss = 0.0
            _, _, max_r, max_c = F0.shape
            for i in range(len(handle_points)):
                pi, ti = handle_points[i], target_points[i]
                # skip if the distance between target and source is less than 1
                if (ti - pi).norm() < 2.:
                    continue

                di = (ti - pi) / (ti - pi).norm() * min(4,(ti-pi).norm())

                # motion supervision
                # with boundary protection
                r1, r2 = max(0,int(pi[0])-args.r_m), min(max_r,int(pi[0])+args.r_m+1)
                c1, c2 = max(0,int(pi[1])-args.r_m), min(max_c,int(pi[1])+args.r_m+1)
                
                # default setting in DragDiffusion
                # f0_patch = F1[:,:,r1:r2, c1:c2].detach()
                f1_patch = interpolate_feature_patch(F1,r1+di[0],r2+di[0],c1+di[1],c2+di[1])

                pi = handle_points_init[i]
                r1, r2 = max(0,int(pi[0])-args.r_m), min(max_r,int(pi[0])+args.r_m+1)
                c1, c2 = max(0,int(pi[1])-args.r_m), min(max_c,int(pi[1])+args.r_m+1)
                f0_patch = F0[:,:,r1:r2, c1:c2].detach()
                # original code, without boundary protection
                # f0_patch = F1[:,:,int(pi[0])-args.r_m:int(pi[0])+args.r_m+1, int(pi[1])-args.r_m:int(pi[1])+args.r_m+1].detach()
                # f1_patch = interpolate_feature_patch(F1, pi[0] + di[0], pi[1] + di[1], args.r_m)
                # import pdb;pdb.set_trace()
                if f1_patch.shape != f0_patch.shape:
                    loss += ((2*args.r_m+1)**2)*(f0_patch.mean()-f1_patch.mean())
                else:
                    loss += ((2*args.r_m+1)**2)*F.l1_loss(f0_patch, f1_patch)

            # masked region must stay unchanged
            if using_mask:
                loss += args.lam * ((x_prev_updated-x_prev_0)*(1.0-interp_mask)).abs().sum()
            # loss += args.lam * ((init_code_orig-init_code)*(1.0-interp_mask)).abs().sum()
            ######################################### clip guidance part ####################################
            def cal_global_grad(init_code,args):
                z = init_code.detach().requires_grad_(True)
               
                image_aug  = ImageAugmentations(224, 1).to(device)
                unet_output= model.unet(z,
                                    t,
                                    encoder_hidden_states=text_embeddings,
                                    return_intermediates=False
                                    )
                x_prev_0,pred_x0 = model.step(unet_output, t, z)
                pred_image = 1 / 0.18215 * pred_x0.half()
                pred_image = model.vae.decode(pred_image)['sample']
                pred_image = (pred_image / 2 + 0.5).clamp(0, 1)

                clip_normalize = transforms.Normalize(
                mean=[0.48145466, 0.4578275, 0.40821073], std=[0.26862954, 0.26130258, 0.27577711]
                        )


                aug_img = image_aug(pred_image).add(1).div(2)

                clip_img = clip_normalize(aug_img)    
                print(f'prompt for finetune is :{args.drag_prompt}')
                lora_prompt = args.drag_prompt
                lora_prompt = clip.tokenize(lora_prompt).to(device)
                image_features = clip_model.encode_image(clip_img)
                text_features = clip_model.encode_text(lora_prompt)
                x = F.normalize(image_features,dim=-1)
                y = F.normalize(text_features,dim=-1)
                clip_loss = 1-(x@y.t()).squeeze()
                clip_loss =args.clip_loss_coef*clip_loss
                print(f'clip loss:{clip_loss}')
                grad_global = torch.autograd.grad(clip_loss,z)[0]
                del pred_image,clip_img,aug_img,image_features,text_features
                # clip_loss.backward(retain_graph=True)
                # grad_global = init_code.grad
                # optimizer.zero_grad()
                return grad_global
            
            if args.drag_prompt != "":
                grad_global = cal_global_grad(init_code,args)

            #################################################################################################
                print('loss total=%f'%(loss.item()))
                loss.backward()
                grad_direction = init_code.grad
                cosine = torch.cosine_similarity(grad_global.view(1,-1),grad_direction.view(1,-1))
                sine = (1-cosine**2)**0.5
                print(f'cosine between two gradients:{cosine}')
                pro_lambda=args.fuse_cof 
                print(f'prompt lambda for global local fuse:{pro_lambda}')
                if cosine >=0:
                    grad_prompt = grad_direction + pro_lambda*sine*grad_global
                else:
                    grad_prompt = grad_direction + pro_lambda*cosine*grad_global   
            # grad_prompt = grad_direction + pro_lambda*cosine*grad_global   
  
                init_code.grad = grad_prompt

        optimizer.step()

        optimizer.zero_grad()

    return init_code

def drag_diffusion_update_gen(model,
                              init_code,
                              text_embeddings,
                              t,
                              handle_points,
                              target_points,
                              mask,
                              args):

    assert len(handle_points) == len(target_points), \
        "number of handle point must equals target points"
    if text_embeddings is None:
        text_embeddings = model.get_text_embeddings(args.prompt)

    # positive prompt embedding
    if args.guidance_scale > 1.0:
        unconditional_input = model.tokenizer(
            [args.neg_prompt],
            padding="max_length",
            max_length=77,
            return_tensors="pt"
        )
        unconditional_emb = model.text_encoder(unconditional_input.input_ids.to(text_embeddings.device))[0].detach()
        text_embeddings = torch.cat([unconditional_emb, text_embeddings], dim=0)

    # the init output feature of unet
    with torch.no_grad():
        if args.guidance_scale > 1.:
            model_inputs_0 = copy.deepcopy(torch.cat([init_code] * 2))
        else:
            model_inputs_0 = copy.deepcopy(init_code)
        unet_output, F0 = model.forward_unet_features(model_inputs_0, t, encoder_hidden_states=text_embeddings,
            layer_idx=args.unet_feature_idx, interp_res_h=args.sup_res_h, interp_res_w=args.sup_res_w)
        if args.guidance_scale > 1.:
            # strategy 1: discard the unconditional branch feature maps
            # F0 = F0[1].unsqueeze(dim=0)
            # strategy 2: concat pos and neg branch feature maps for motion-sup and point tracking
            # F0 = torch.cat([F0[0], F0[1]], dim=0).unsqueeze(dim=0)
            # strategy 3: concat pos and neg branch feature maps with guidance_scale consideration
            coef = args.guidance_scale / (2*args.guidance_scale - 1.0)
            F0 = torch.cat([(1-coef)*F0[0], coef*F0[1]], dim=0).unsqueeze(dim=0)

            unet_output_uncon, unet_output_con = unet_output.chunk(2, dim=0)
            unet_output = unet_output_uncon + args.guidance_scale * (unet_output_con - unet_output_uncon)
        x_prev_0,_ = model.step(unet_output, t, init_code)
        # init_code_orig = copy.deepcopy(init_code)

    # prepare optimizable init_code and optimizer
    init_code.requires_grad_(True)
    optimizer = torch.optim.Adam([init_code], lr=args.lr)

    # prepare for point tracking and background regularization
    handle_points_init = copy.deepcopy(handle_points)
    interp_mask = F.interpolate(mask, (init_code.shape[2],init_code.shape[3]), mode='nearest')
    using_mask = interp_mask.sum() != 0.0

    # prepare amp scaler for mixed-precision training
    scaler = torch.cuda.amp.GradScaler()
    for step_idx in range(args.n_pix_step):
        with torch.autocast(device_type='cuda', dtype=torch.float16):
            if args.guidance_scale > 1.:
                model_inputs = init_code.repeat(2,1,1,1)
            else:
                model_inputs = init_code
            unet_output, F1 = model.forward_unet_features(model_inputs, t, encoder_hidden_states=text_embeddings,
                layer_idx=args.unet_feature_idx, interp_res_h=args.sup_res_h, interp_res_w=args.sup_res_w)
            if args.guidance_scale > 1.:
                # strategy 1: discard the unconditional branch feature maps
                # F1 = F1[1].unsqueeze(dim=0)
                # strategy 2: concat positive and negative branch feature maps for motion-sup and point tracking
                # F1 = torch.cat([F1[0], F1[1]], dim=0).unsqueeze(dim=0)
                # strategy 3: concat pos and neg branch feature maps with guidance_scale consideration
                coef = args.guidance_scale / (2*args.guidance_scale - 1.0)
                F1 = torch.cat([(1-coef)*F1[0], coef*F1[1]], dim=0).unsqueeze(dim=0)

                unet_output_uncon, unet_output_con = unet_output.chunk(2, dim=0)
                unet_output = unet_output_uncon + args.guidance_scale * (unet_output_con - unet_output_uncon)
            x_prev_updated,_ = model.step(unet_output, t, init_code)

            # do point tracking to update handle points before computing motion supervision loss
            if step_idx != 0:
                handle_points = fast_point_tracking(F0, F1, handle_points, handle_points_init, args)
                print('new handle points', handle_points)

            # break if all handle points have reached the targets
            if check_handle_reach_target(handle_points, target_points):
                break

            loss = 0.0
            _, _, max_r, max_c = F0.shape
            for i in range(len(handle_points)):
                pi, ti = handle_points[i], target_points[i]
                # skip if the distance between target and source is less than 1
                if (ti - pi).norm() < 2.:
                    continue

                di = (ti - pi) / (ti - pi).norm()

                # motion supervision
                # with boundary protection
                r1, r2 = max(0,int(pi[0])-args.r_m), min(max_r,int(pi[0])+args.r_m+1)
                c1, c2 = max(0,int(pi[1])-args.r_m), min(max_c,int(pi[1])+args.r_m+1)
                f0_patch = F1[:,:,r1:r2, c1:c2].detach()
                f1_patch = interpolate_feature_patch(F1,r1+di[0],r2+di[0],c1+di[1],c2+di[1])

                # original code, without boundary protection
                # f0_patch = F1[:,:,int(pi[0])-args.r_m:int(pi[0])+args.r_m+1, int(pi[1])-args.r_m:int(pi[1])+args.r_m+1].detach()
                # f1_patch = interpolate_feature_patch(F1, pi[0] + di[0], pi[1] + di[1], args.r_m)

                loss += ((2*args.r_m+1)**2)*F.l1_loss(f0_patch, f1_patch)

            # masked region must stay unchanged
            if using_mask:
                loss += args.lam * ((x_prev_updated-x_prev_0)*(1.0-interp_mask)).abs().sum()
            # loss += args.lam * ((init_code_orig - init_code)*(1.0-interp_mask)).abs().sum()
            print('loss total=%f'%(loss.item()))

        scaler.scale(loss).backward()
        scaler.step(optimizer)
        scaler.update()
        optimizer.zero_grad()

    return init_code

