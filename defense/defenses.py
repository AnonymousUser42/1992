import os
import gc
import torch
import numpy as np
from torchvision.utils import save_image
import quadprog

# soteria
def defense_soteria(args, gt_images, gt_labels, model, loss_fn, device, layer_num, percent_num=1, perturb_imprint=False):
    ## compute ||d(f(r))/dX||/||r||
    ## use ||r||/||d(f(r))/dX|| to approximate ||r(d(f(r))/dX)^-1||
    model.eval()
    model.zero_grad()
    gt_images.requires_grad = True
    if perturb_imprint:
        out, _, feature_fc1_graph = model(gt_images)  # perturb the imprint module
    else:
        out, feature_fc1_graph, _ = model(gt_images)
    deviation_f1_target = torch.zeros_like(feature_fc1_graph)
    deviation_f1_x_norm = torch.zeros_like(feature_fc1_graph)

    for f in range(deviation_f1_x_norm.size(1)):
        deviation_f1_target[:,f] = 1
        feature_fc1_graph.backward(deviation_f1_target, retain_graph=True)
        deviation_f1_x = gt_images.grad.data  # df(x)/dx
        if args.cost_fn == 'l2':
            deviation_f1_x_norm[:,f] = torch.norm(
                deviation_f1_x.view(deviation_f1_x.size(0), -1), dim=1)/(feature_fc1_graph.data[:,f])
        else:
            deviation_f1_x_norm[:,f] = torch.norm(
                deviation_f1_x.view(deviation_f1_x.size(0), -1), dim=1)/(feature_fc1_graph.data[:,f] + 0.1)
        model.zero_grad()
        gt_images.grad.data.zero_()
        deviation_f1_target[:,f] = 0
        del deviation_f1_x
        torch.cuda.empty_cache()
        gc.collect()

    # prune r_i corresponding to smallest ||d(f(r_i))/dX||/||r_i||
    deviation_f1_x_norm_sum = deviation_f1_x_norm.sum(axis=0)
    thresh = np.percentile(deviation_f1_x_norm_sum.flatten().cpu().numpy(), percent_num)
    mask = np.where(abs(deviation_f1_x_norm_sum.cpu()) < thresh, 0, 1).astype(np.float32)
    # print(sum(mask))

    gt_loss = loss_fn(out, gt_labels)
    gt_gradients = torch.autograd.grad(gt_loss, model.parameters())
    gt_gradient = [grad.detach().clone() for grad in gt_gradients]
    # perturb gradtients
    gt_gradient[layer_num] = gt_gradient[layer_num] * torch.Tensor(mask).to(device)
    del deviation_f1_target, deviation_f1_x_norm
    del deviation_f1_x_norm_sum, feature_fc1_graph
    torch.cuda.empty_cache()
    gc.collect()


    return gt_gradient

# model compression
def defense_compression(gt_gradients, device, percent_num=10):

    gt_gradient = [grad.detach().clone() for grad in gt_gradients]
    for i in range(len(gt_gradient)):
        grad_tensor = gt_gradient[i].cpu().numpy()
        flattened_weights = np.abs(grad_tensor.flatten())
        # Generate the pruning threshold according to 'prune by percentage'.
        thresh = np.percentile(flattened_weights, percent_num)
        grad_tensor = np.where(abs(grad_tensor) < thresh, 0, grad_tensor)
        gt_gradient[i] = torch.Tensor(grad_tensor).to(device)

    return gt_gradient

# differential privacy
def defense_dp(gt_gradients, device, loc, scale, noise_name):

    gt_gradient = [grad.detach().clone() for grad in gt_gradients]
    for i in range(len(gt_gradient)):
        grad_tensor = gt_gradient[i].cpu().numpy()
        if noise_name == 'Laplace':
            noise = np.random.laplace(loc, scale, size=grad_tensor.shape)
        else:
            noise = np.random.normal(loc, scale, size=grad_tensor.shape)
        grad_tensor = grad_tensor + noise
        gt_gradient[i] = torch.Tensor(grad_tensor).to(device)

    return gt_gradient

# compute tv
def total_variation(x):
    """Anisotropic TV."""
    dx = torch.mean(torch.abs(x[:, :, :, :-1] - x[:, :, :, 1:]))
    dy = torch.mean(torch.abs(x[:, :, :-1, :] - x[:, :, 1:, :]))
    return dx + dy

# projection
def project2cone2(gradient, memories, margin=0.5, eps=1e-3):
    memories_np = memories.cpu().t().contiguous().double().numpy()
    gradient_np = gradient.cpu().contiguous().view(-1).double().numpy()
    # print("memories_np shape:{}".format(memories_np.shape))
    # print("gradient_np shape:{}".format(gradient_np.shape))
    t = memories_np.shape[0]  # task mums
    P = np.dot(memories_np, memories_np.transpose())
    P = 0.5 * (P + P.transpose()) + np.eye(t) * eps
    q = np.dot(memories_np, gradient_np) * -1
    G = np.eye(t)
    h = np.zeros(t) + margin
    v = quadprog.solve_qp(P, q, G, h)[0] # get the optimal solution of v~
    x = np.dot(v, memories_np) + gradient_np  # g~ = v*GT +g
    # gradient.copy_(torch.Tensor(x).view(-1))
    new_grad = torch.Tensor(x).view(-1)
    return new_grad


# optimize-based defense for all attacks
def defense_closure(args, model, optimizer, loss_fn, sen_img, sen_label, adv_img, adv_label):
    def closure():
        optimizer.zero_grad()
        model.zero_grad()

        total_loss = 0.
        for sk in range(args.num_sen):
            # print('sensitive data {:d} with label {:d}'.format(sk, sen_label[sk:sk+1].item()))
            sen_out = model(sen_img[sk:sk+1]) # sen_out, sen_fea, sen_tmp
            sen_loss = loss_fn(sen_out[0], sen_label[sk:sk+1])
            sen_gradients = torch.autograd.grad(sen_loss, model.parameters(), create_graph=True)

            for ak in range(args.per_adv):
                pnorm = [0, 0]
                rec_loss = 0.
                # print('modified data {:d} with label {:d}'.format(
                #     ak + sk * args.per_adv, adv_label[ak + sk * args.per_adv: ak + 1 + sk * args.per_adv].item()))
                adv_out = model(adv_img[ak + sk * args.per_adv: ak + 1 + sk * args.per_adv]) # adv_out, adv_fea, adv_tmp
                loss = loss_fn(adv_out[0], adv_label[ak + sk * args.per_adv: ak + 1 + sk * args.per_adv])
                adv_dydw = torch.autograd.grad(loss, model.parameters(), create_graph=True)

                for gx, gy in zip(sen_gradients, adv_dydw):
                    rec_loss += (gx * (args.deg*gy)).sum()
                    pnorm[0] += gx.pow(2).sum()
                    pnorm[1] += (args.deg*gy).pow(2).sum()
                g_sim = 1 - rec_loss / (torch.sqrt(pnorm[0]) * torch.sqrt(pnorm[1]) + 1e-8)

                xsim = torch.norm(adv_img[ak + sk * args.per_adv: ak + 1 + sk * args.per_adv] - sen_img[sk:sk+1])
                fxsim = torch.norm(adv_out[args.min_idx] - sen_out[args.min_idx].clone().detach())
                # if args.vis:
                #     print('x_sim {:.4f}, fx_sim {:.4f}'.format(xsim.detach(), fxsim.detach()))
                x_sim = args.alpha / (xsim + 1e-8)
                fx_sim = args.beta * fxsim
                total_loss += g_sim + x_sim + fx_sim
                # if args.vis:
                #     print('x_sim {:.4f}, fx_sim {:.4f}, g_sim {:.4f}, total_loss {:.4f}'.format(
                #         x_sim.item(), fx_sim.item(), g_sim.item(), total_loss.item()))

        if args.detv > 0:
            total_loss += args.detv * total_variation(adv_img)
        total_loss.backward(retain_graph=True)

        return total_loss

    return closure

def defense_optim(args, model, loss_fn, gt_gradients, gt_imgs, gt_labels, dm, ds, device):

    # original gradient g excluding the sensitive data
    ori_g = [grad.detach().view(-1) for grad in gt_gradients]
    ori_g = torch.cat(ori_g)
    torch.cuda.empty_cache()
    gc.collect()

    # modify data
    sen_img = gt_imgs[-args.num_sen:]
    sen_label = gt_labels[-args.num_sen:]
    adv_img = (gt_imgs[-args.num_sen - (args.num_sen * args.per_adv):-args.num_sen]).clone().detach().to(device).requires_grad_(True)
    adv_label = (gt_labels[-args.num_sen - (args.num_sen * args.per_adv):-args.num_sen]).clone().detach().to(device)
    tmp_label = (gt_labels[-args.num_sen - (args.num_sen * args.per_adv):-args.num_sen]).clone().detach().to(device)
    for sk in range(args.num_sen):
        tmp_label[sk*args.per_adv: sk*args.per_adv + args.per_adv] = sen_label[sk].repeat(args.per_adv)

    optimizer = torch.optim.Adam([adv_img], lr=args.delr)
    if args.delr_decay:
        scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer,
                                                         milestones=[args.demax_iter // 2.667,
                                                                     args.demax_iter // 1.6,
                                                                     args.demax_iter // 1.142],
                                                         gamma=0.1)   # 3/8 5/8 7/8
    for j in range(args.demax_iter):
        closure = defense_closure(args, model, optimizer, loss_fn,
                                  sen_img, sen_label,
                                  adv_img, adv_label)
        rec_loss = optimizer.step(closure)
        if args.delr_decay:
            scheduler.step()
        # if ((j + 1 == args.demax_iter) or j % 500 == 0) and args.vis:
        #     print(f'Defense - Iter-{j}: Rec_loss-{rec_loss.item():2.6f}.')
        if args.deboxed:
            with torch.no_grad():
                adv_img = torch.clamp(adv_img, -dm / ds, (1 - dm) / ds)
    adv_imgs = torch.cat([gt_imgs[:-args.num_sen - (args.num_sen * args.per_adv)], adv_img, gt_imgs[-args.num_sen:]]).to(device)
    adv_labels = torch.cat([gt_labels[:-args.num_sen - (args.num_sen * args.per_adv)], adv_label, gt_labels[-args.num_sen:]]).to(device)
    if args.vis:
        print('adv_label: ', adv_label)
        print('final_labels: ', adv_labels)
        gt_denormalized = torch.clamp(adv_img * ds + dm, 0, 1)
        save_image(gt_denormalized, os.path.join(args.output_dir, 'adv.png'))
        adv_denormalized = torch.clamp(adv_imgs * ds + dm, 0, 1)
        save_image(adv_denormalized, os.path.join(args.output_dir, 'final.png'))

    # new gradient \ddot{g} after modifying the data
    adv_out, _, _ = model(adv_imgs)
    if args.mixup:
        if adv_imgs.size(0) - args.per_adv * args.num_sen - args.num_sen == 0:
            loss = ((args.lamb * loss_fn(adv_out[-args.num_sen - (args.num_sen * args.per_adv):-args.num_sen], adv_label) \
                   + (1 - args.lamb) * loss_fn(adv_out[-args.num_sen - (args.num_sen * args.per_adv):-args.num_sen], tmp_label)) \
                   * (args.per_adv * args.num_sen) + args.num_sen * loss_fn(adv_out[-args.num_sen:], sen_label)) / adv_imgs.size(0)
        else:
            loss = (
                (adv_imgs.size(0) - args.per_adv * args.num_sen - args.num_sen) \
                * loss_fn(adv_out[:-args.num_sen - (args.num_sen * args.per_adv)],
                          adv_labels[:-args.num_sen - (args.num_sen * args.per_adv)]) \
                + (args.lamb * loss_fn(adv_out[-args.num_sen - (args.num_sen * args.per_adv):-args.num_sen], adv_label) \
                + (1 - args.lamb) * loss_fn(adv_out[-args.num_sen - (args.num_sen * args.per_adv):-args.num_sen], tmp_label)) \
                * (args.per_adv * args.num_sen) + args.num_sen * loss_fn(adv_out[-args.num_sen:], sen_label)) / adv_imgs.size(0)
    else:
        loss = loss_fn(adv_out, adv_labels)
    # if args.vis:
    #     print('mixup_loss', loss.item())
    adv_dydw = torch.autograd.grad(loss, model.parameters())
    adv_g = [grad.detach().view(-1) for grad in adv_dydw]
    adv_g = torch.cat(adv_g)

    # check if gradient violates constrains
    if args.dataset == 'ImageNet' or args.dataset == 'HAM10000':
        dotg = torch.mm(adv_g.unsqueeze(0), ori_g.unsqueeze(1))
    else:
        dotg = ori_g * adv_g  # ~ projection
    if (dotg < 0).sum() != 0:
        new_grad = project2cone2(adv_g.unsqueeze(0), ori_g.unsqueeze(1))
        # overwrite current param
        pointer = 0
        dy_dx = []
        for n, p in model.named_parameters():
            num_param = p.numel()
            # p.grad.copy_(new_grad[pointer: pointer + num_param].view_as(p))
            # dy_dx.append(p.grad)
            dy_dx.append(new_grad[pointer: pointer + num_param].view_as(p).to(device))
            pointer += num_param
        gt_gradient = dy_dx
    else:
        gt_gradient = adv_dydw

    return gt_gradient, adv_imgs, adv_labels

