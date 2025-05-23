import numpy as np
import torch
import torch.nn.functional as F
torch.set_default_tensor_type('torch.cuda.FloatTensor')
from torch.nn import L1Loss
from torch.nn import MSELoss

def get_warmup_lr(epoch, max_epochs=1000, warmup_ratio=0.1, strategy="linear_cosine"):
    warmup_epochs = int(max_epochs * warmup_ratio)
    initial_lr = 0.001
    target_lr = 1.0

    # Warm-up 阶段
    if epoch < warmup_epochs:
        if strategy.startswith("linear"):
            return initial_lr + (target_lr - initial_lr) * (epoch / warmup_epochs)  # 线性增长[1,2](@ref)
        elif strategy.startswith("exponential"):
            return initial_lr * (target_lr / initial_lr) ** (epoch / warmup_epochs)  # 指数增长[2](@ref)
        elif strategy.startswith("cosine"):
            return initial_lr + 0.5 * (target_lr - initial_lr) * (
                        1 - np.cos(np.pi * epoch / warmup_epochs))  # 余弦增长[3](@ref)

    # 衰减阶段
    decay_epochs = max_epochs - warmup_epochs
    if strategy.endswith("cosine"):
        return target_lr * 0.5 * (1 + np.cos(np.pi * (epoch - warmup_epochs) / decay_epochs))  # 余弦衰减[3](@ref)
    elif strategy.endswith("step"):
        return target_lr * (0.1 ** ((epoch - warmup_epochs) // 200))  # 每200epoch衰减0.1倍[6](@ref)
    elif strategy.endswith("none"):
        return target_lr  # 无衰减

def sparsity(arr, batch_size, lamda2):  # arr1.shape=[1024, ], abn_scores
    loss = torch.mean(torch.norm(arr, dim=0))
    return lamda2*loss


def smooth(arr, lamda1):  # arr1.shape=[1024, ], abn_scores
    arr2 = torch.zeros_like(arr)
    arr2[:-1] = arr[1:]  # f_t
    arr2[-1] = arr[-1]  # f_t-1

    loss = torch.sum((arr2-arr)**2)

    return lamda1*loss


def l1_penalty(var):
    return torch.mean(torch.norm(var, dim=0))


class SigmoidMAELoss(torch.nn.Module):
    def __init__(self):
        super(SigmoidMAELoss, self).__init__()
        from torch.nn import Sigmoid
        self.__sigmoid__ = Sigmoid()
        self.__l1_loss__ = MSELoss()

    def forward(self, pred, target):
        return self.__l1_loss__(pred, target)


class SigmoidCrossEntropyLoss(torch.nn.Module):
    # Implementation Reference: http://vast.uccs.edu/~adhamija/blog/Caffe%20Custom%20Layer.html
    def __init__(self):
        super(SigmoidCrossEntropyLoss, self).__init__()

    def forward(self, x, target):
        tmp = 1 + torch.exp(- torch.abs(x))
        return torch.abs(torch.mean(- x * target + torch.clamp(x, min=0) + torch.log(tmp)))


class RTFM_loss(torch.nn.Module):
    def __init__(self, alpha, margin, normal_weight, abnormal_weight):
        super(RTFM_loss, self).__init__()
        self.alpha = alpha
        self.margin = margin
        self.sigmoid = torch.nn.Sigmoid()
        self.mae_criterion = SigmoidMAELoss()
        self.normal_weight = normal_weight
        self.abnormal_weight = abnormal_weight
        if normal_weight == 1 and abnormal_weight == 1:
            self.criterion = torch.nn.BCELoss()
        else:
            self.criterion = None

    def forward(self, score_normal, score_abnormal, nlabel, alabel, feat_n, feat_a):
        label = torch.cat((nlabel, alabel), 0)  # shape=[64,]
        score_abnormal = score_abnormal
        score_normal = score_normal

        score = torch.cat((score_normal, score_abnormal), 0)
        score = score.squeeze()  # shape=[64,]

        label = label.cuda()

        if self.criterion:
            loss_cls = self.criterion(score, label)  # Section 3.4, BCE loss in the score space, score是基于每个类别的top 3的分数求平均算出来的
        else:
            # calculate for label, if label is 1, then weight is abnormal_weight, else weight is normal_weight
            weight = self.abnormal_weight * label + self.normal_weight * (1 - label)
            self.criterion = torch.nn.BCELoss(weight)
            loss_cls = self.criterion(score, label)

        # abnormal scores should be as large as possible (so we add a margin for loss_abn) while normal scores should be as large as possible
        loss_abn = torch.abs(self.margin - torch.norm(torch.mean(feat_a, dim=1), p=2, dim=1))  # shape=[320,3,2048], Formula (2), feat_a的top3平均后计算l2 norm, shape=[320,]
        loss_nor = torch.norm(torch.mean(feat_n, dim=1), p=2, dim=1)  # Formula (2), feat_n的top3平均后计算l2 norm, shape=[320,]

        loss_rtfm = torch.mean((loss_abn + loss_nor) ** 2)  # Section 3.3, 是否等价？

        loss_total = loss_cls + self.alpha * loss_rtfm

        return loss_total


def train(nloader, aloader, model, args, optimizer, viz, device,percent,logger=None,step=None):
    with torch.set_grad_enabled(True):
        model.train()
        batch_size = args.batch_size
        ninput, ntext, nlabel,sources = next(nloader)  # ninput.shape=[32,10,32,2048], nlabel.shape=[32,]
        ainput, atext, alabel,sources = next(aloader)  # ainput.shape=[32,10,32,2048], alabel.shape=[32,]

        input = torch.cat((ninput, ainput), 0).to(device)  # input.shape=[64,10,32,2048], 第一维是batch_size * 2, 第三维是snippets数
        text = torch.cat((ntext, atext), 0).to(device)

        score_abnormal, score_normal, feat_select_abn, feat_select_normal, feat_abn_bottom, \
        feat_normal_bottom, scores, scores_nor_bottom, scores_nor_abn_bag, _ = model(input, text)
        # score_abnormal, score_normal, feat_select_abn, feat_select_normal, feat_abn_bottom, \
        # feat_normal_bottom, scores, scores_nor_bottom, scores_nor_abn_bag, = model(input, text)  # b*32 x 2048

        # 动态损失加权
        a_mask = (sources == 0)
        b_mask = (sources == 1)

        scores = scores.view(batch_size * 32 * 2, -1)  # scores.shape=[64,32,1] -> [2048,1]

        scores = scores.squeeze()
        abn_scores = scores[batch_size * 32:]  # abn_scores.shape=[1024,]

        nlabel = nlabel[0:batch_size]
        alabel = alabel[0:batch_size]

        loss_criterion = RTFM_loss(args.alpha, 100, args.normal_weight, args.abnormal_weight)  # margin=100
        loss_sparse = sparsity(abn_scores, batch_size, 8e-3)  # Section 3.4, sparsity regularisation
        loss_smooth = smooth(abn_scores, 8e-4)  # Section 3.4, temporal smoothness
        if args.extra_loss:
            cost = loss_criterion(score_normal, score_abnormal, nlabel, alabel, feat_select_normal,
                                  feat_select_abn) + loss_smooth + 1*loss_sparse
        else:
            cost = loss_criterion(score_normal, score_abnormal, nlabel, alabel, feat_select_normal, feat_select_abn)

        viz.plot_lines('loss', cost.item())
        viz.plot_lines('smooth loss', loss_smooth.item())
        viz.plot_lines('sparsity loss', loss_sparse.item())

        # 应用不同权重
        VLR_Warmup_lrRate=1.0
        if args.VLR_Strategy != 'None':
            VLR_Warmup_lrRate = get_warmup_lr(step+100, max_epochs=args.max_epoch, warmup_ratio=0.1,
                                              strategy=args.VLR_Strategy)
        loss_a = cost * a_mask.float().mean()
        loss_b = cost * b_mask.float().mean() * args.VLR * VLR_Warmup_lrRate  # args.VLR: 虚拟数据集学习率倍率

        # 组合总损失
        total_loss = loss_a + loss_b # + args.beta * LAT_loss + triplet_loss

        optimizer.zero_grad()  # 将模型的参数梯度初始化为0
        # cost.backward()  # 反向传播计算梯度
        total_loss.backward()
        optimizer.step()  # 更新所有参数

        if logger:
            logger.log(f"Epoch {step}  loss: {cost.item():.4f}  smooth loss: {loss_smooth.item():.4f}  sparsity loss: {loss_sparse.item():.4f}")


