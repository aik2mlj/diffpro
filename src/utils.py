from dirs import *
import numpy as np
import pickle
import os
import pretty_midi as pm
import torch
from dl_modules import PianoTreeEncoder, PianoTreeDecoder
from collections import OrderedDict
from torch.distributions import Normal, kl_divergence


def load_pretrained_pnotree_enc_dec(fpath, max_simu_note, device):
    pnotree_enc = PianoTreeEncoder(device=device, max_simu_note=max_simu_note)
    pnotree_dec = PianoTreeDecoder(device=device, max_simu_note=max_simu_note)
    checkpoint = torch.load(fpath)
    enc_checkpoint = OrderedDict()
    dec_checkpoint = OrderedDict()
    enc_param_list = [
        "note_embedding", "enc_notes_gru", "enc_time_gru", "linear_mu", "linear_std"
    ]
    for k, v in checkpoint.items():
        part = k.split('.')[0]
        # print(part)
        # name = '.'.join(k.split('.')[1 :])
        # print(part, name)
        if part in enc_param_list:
            enc_checkpoint[k] = v
            if part == "note_embedding":
                dec_checkpoint[k] = v
        else:
            dec_checkpoint[k] = v
    pnotree_enc.load_state_dict(enc_checkpoint)
    pnotree_dec.load_state_dict(dec_checkpoint)
    pnotree_enc.to(device)
    pnotree_dec.to(device)
    return pnotree_enc, pnotree_dec


def save_dict(path, dict_file):
    with open(path, "wb") as handle:
        pickle.dump(dict_file, handle, protocol=pickle.HIGHEST_PROTOCOL)


def read_dict(path):
    with open(path, "rb") as handle:
        return pickle.load(handle)


def nested_map(struct, map_fn):
    """This is for trasfering into cuda device"""
    if isinstance(struct, tuple):
        return tuple(nested_map(x, map_fn) for x in struct)
    if isinstance(struct, list):
        return [nested_map(x, map_fn) for x in struct]
    if isinstance(struct, dict):
        return {k: nested_map(v, map_fn) for k, v in struct.items()}
    return map_fn(struct)


def standard_normal(shape):
    N = Normal(torch.zeros(shape), torch.ones(shape))
    if torch.cuda.is_available():
        N.loc = N.loc.cuda()
        N.scale = N.scale.cuda()
    return N


def kl_with_normal(dist):
    shape = dist.mean.size(-1)
    normal = standard_normal(shape)
    kl = kl_divergence(dist, normal).mean()
    return kl


def nmat_to_pianotree_repr(
    nmat,
    n_step=32,
    max_note_count=20,
    dur_pad_ind=2,
    min_pitch=0,
    pitch_sos_ind=128,
    pitch_eos_ind=129,
    pitch_pad_ind=130,
):
    """
    Convert the input note matrix to pianotree representation.
    Input: (N, 3), 3 for onset, pitch, duration. o and d are in time steps.
    """

    pnotree = np.ones((n_step, max_note_count, 6), dtype=np.int64) * dur_pad_ind
    pnotree[:, :, 0] = pitch_pad_ind
    pnotree[:, 0, 0] = pitch_sos_ind

    cur_idx = np.ones(n_step, dtype=np.int64)
    for o, p, d in nmat:
        pnotree[o, cur_idx[o], 0] = p - min_pitch

        # e.g., d = 4 -> bin_str = '00011'
        d = min(d, 32)
        bin_str = np.binary_repr(int(d) - 1, width=5)
        pnotree[o, cur_idx[o],
                1 :] = np.fromstring(" ".join(list(bin_str)), dtype=np.int64, sep=" ")

        # FIXME: when more than `max_note_count` notes are played in one step
        if cur_idx[o] < max_note_count - 1:
            cur_idx[o] += 1
        else:
            print(f"more than max_note_count {max_note_count} occur!")

    pnotree[np.arange(0, n_step), cur_idx, 0] = pitch_eos_ind
    return pnotree


def pianotree_pitch_shift(pnotree, shift):
    pnotree = pnotree.copy()
    pnotree[pnotree[:, :, 0] < 128, 0] += shift
    return pnotree


def pr_mat_pitch_shift(pr_mat, shift):
    pr_mat = pr_mat.copy()
    pr_mat = np.roll(pr_mat, shift, -1)
    return pr_mat


def chd_pitch_shift(chd, shift):
    chd = chd.copy()
    chd[:, 0] = (chd[:, 0] + shift) % 12
    chd[:, 1 : 13] = np.roll(chd[:, 1 : 13], shift, axis=-1)
    chd[:, -1] = (chd[:, -1] + shift) % 12
    return chd


def chd_to_onehot(chd):
    n_step = chd.shape[0]
    onehot_chd = np.zeros((n_step, 36), dtype=np.int64)
    onehot_chd[np.arange(n_step), chd[:, 0]] = 1
    onehot_chd[:, 12 : 24] = chd[:, 1 : 13]
    onehot_chd[np.arange(n_step), 24 + chd[:, -1]] = 1
    return onehot_chd


def onehot_to_chd(onehot):
    n_step = onehot.shape[0]
    chd = np.zeros((n_step, 14), dtype=np.int64)
    chd[:, 0] = np.argmax(onehot[:, 0 : 12], axis=1)
    chd[:, 1 : 13] = onehot[:, 12 : 24]
    chd[:, 13] = np.argmax(onehot[:, 24 : 36], axis=1)
    return chd


def nmat_to_pr_mat_repr(nmat, n_step=32):
    pr_mat = np.zeros((n_step, 128), dtype=np.int64)
    for o, p, d in nmat:
        pr_mat[o, p] = d
    return pr_mat


def nmat_to_rhy_array(nmat, n_step=32):
    """Compute onset track of from melody note matrix."""
    pr_mat = np.zeros(n_step, dtype=np.int64)
    for o, _, _ in nmat:
        pr_mat[o] = 1
    return pr_mat


def estx_to_midi_file(est_x, fpath, labels=None):
    # print(f"est_x with shape {est_x.shape} to midi file {fpath}")  # (#, 32, 15, 6)
    # pr_mat3d is a (32, max_note_count, 6) matrix. In the last dim,
    # the 0th column is for pitch, 1: 6 is for duration in binary repr. Output is
    # padded with <sos> and <eos> tokens in the pitch column, but with pad token
    # for dur columns.
    midi = pm.PrettyMIDI()
    piano_program = pm.instrument_name_to_program("Acoustic Grand Piano")
    piano = pm.Instrument(program=piano_program)
    t = 0
    for two_bar_ind, two_bars in enumerate(est_x):
        for step_ind, step in enumerate(two_bars):
            for kth_key in step:
                assert len(kth_key) == 6
                if not (kth_key[0] >= 0 and kth_key[0] <= 127):
                    # rest key
                    # print(f"({two_bar_ind}, {step_ind}, somekey, 0): {kth_key[0]}")
                    continue

                # print(f"({two_bar_ind}, {step_ind}, somekey, 0): {kth_key[0]}")
                dur = (
                    kth_key[5] + (kth_key[4] << 1) + (kth_key[3] << 2) +
                    (kth_key[2] << 3) + (kth_key[1] << 4) + 1
                )
                note = pm.Note(
                    velocity=80,
                    pitch=int(kth_key[0]),
                    start=t + step_ind * 1 / 8,
                    end=min(t + (step_ind + int(dur)) * 1 / 8, t + 4),
                )
                piano.notes.append(note)
        t += 4
    midi.instruments.append(piano)

    if labels is not None:
        midi.lyrics.clear()
        t = 0
        for label in labels:
            midi.lyrics.append(pm.Lyric(label, t))
            t += 4

    midi.write(fpath)


if __name__ == "__main__":
    load_pretrained_pnotree_enc_dec(
        "../PianoTree-VAE/model20/train_20-last-model.pt", 20, None
    )
