import pickle
from collections import defaultdict
from itertools import product
from math import log

from gensim.models import Word2Vec
import random
import sys
import os

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
from time import time
import numpy as np
import yaml
import tensorflow as tf
from keras.layers import Dense
from keras.optimizers import RMSprop
from keras.utils import to_categorical
from tqdm import tqdm

from base_models import TGEN_Model, Regressor
from e2e_metrics.metrics.pymteval import BLEUScore
from embedding_extractor import TokEmbeddingSeq2SeqExtractor, DAEmbeddingSeq2SeqExtractor
from scorer_functions import get_identity_score_func
from utils import get_texts_training, RERANK, get_training_das_texts, safe_get_w2v, apply_absts, PAD_TOK, END_TOK, \
    START_TOK


def relative_to_quartiles(scores):
    def to_quartile(x):
        if x < 0.25:
            return 0
        elif x > 0.75:
            return 1
        else:
            return 0.5

    av = sum(scores) / len(scores)
    return [1-to_quartile(x - av + 0.5) for x in scores]


def score_beams_pairwise(beam, pair_wise_model, da_emb):
    text_embedder = pair_wise_model.text_embedder
    da_emb = np.array(da_emb)
    inf_beam_size = len(beam)
    lps = np.array([x[0] for x in beam])
    lps = pair_wise_model.setup_lps(lps)
    da_emb_set = []
    text_1_set = []
    text_2_set = []
    lp_1_set = []
    lp_2_set = []

    for i in range(inf_beam_size):
        text_1 = np.array([text_embedder.pad_to_length(beam[i][1])])
        lp_1 = lps[i]
        for j in range(i + 1, inf_beam_size):
            text_2 = np.array([text_embedder.pad_to_length(beam[j][1])])
            lp_2 = lps[j]

            da_emb_set.append(da_emb)
            text_1_set.append(text_1[0])
            text_2_set.append(text_2[0])
            lp_1_set.append(lp_1)
            lp_2_set.append(lp_2)
    da_emb_set, text_1_set, text_2_set, lp_1_set, lp_2_set = \
        np.array(da_emb_set), np.array(text_1_set), np.array(text_2_set), np.array(lp_1_set), np.array(lp_2_set)

    results = pair_wise_model.predict_order(da_emb_set, text_1_set, text_2_set, lp_1_set, lp_2_set)
    res_pos = 0
    tourn_wins = defaultdict(int)
    for i in range(inf_beam_size):
        for j in range(i + 1, inf_beam_size):
            if results[res_pos][0] > 0.5:
                tourn_wins[i] += 1
            else:
                tourn_wins[j] += 1
            res_pos += 1
    scores = [(tourn_wins[i], beam[i]) for i in range(inf_beam_size)]
    return scores


def score_beams(rescorer, beam, da_emb, i):
    path_scores = []
    logprobs = [x[0] for x in beam]
    for path in beam:
        lp_pos = sum([1 for lp in logprobs if lp > path[0] + 0.000001])
        hyp_score = rescorer(path, lp_pos, da_emb, i, len(beam))
        path_scores.append((hyp_score, path))
    return path_scores


def order_beam_acording_to_rescorer(rescorer, beam, da_emb, i, cfg, out_beam=None):
    if "train_reranker" in cfg:
        quartiles_flag = cfg["train_reranker"]["output_type"] in ['regression_quartiles']
        pairwise_flag = cfg["train_reranker"]["output_type"] in ['pair']
    else:
        quartiles_flag = False
        pairwise_flag = False

    if quartiles_flag:
        scored_finished_beams = score_beams(rescorer, beam, da_emb, i)
        quartiles = relative_to_quartiles([s for (s, t), _ in scored_finished_beams])
        path_scores = [((x, y[1]), z) for x, (y, z) in zip(quartiles, scored_finished_beams)]
    elif pairwise_flag:
        path_scores = score_beams_pairwise(beam, rescorer, da_emb)
    else:
        path_scores = score_beams(rescorer, beam, da_emb, i)

    order = sorted(enumerate(path_scores), reverse=True, key=lambda x: x[1][0])
    if i == 0 and quartiles_flag:
        print("Path scores:", [x for _, (x, _) in order])
    if out_beam is not None:
        beam = out_beam
    result = [beam[i] for i, _ in order]
    return result


def order_beam_after_greedy_complete(rescorer, beam, da_emb, i, enc_outs, seq2seq, max_pred_len, cfg):
    finished_beam = beam.copy()
    for step in range(max_pred_len):
        finished_beam, _ = seq2seq.beam_search_exapand(finished_beam, enc_outs, 1)
        if all([p[1][-1] in seq2seq.text_embedder.end_embs for p in finished_beam]):
            break
    return order_beam_acording_to_rescorer(rescorer, finished_beam, da_emb, i, cfg, beam)


def _run_beam_search_with_rescorer(i, da_emb, paths, enc_outs, beam_size, max_pred_len, seq2seq, cfg,
                                   rescorer=None, greedy_complete=[],
                                   save_progress_file=None):
    end_tokens = seq2seq.text_embedder.end_embs
    for step in range(max_pred_len):
        # expand
        new_paths, tok_probs = seq2seq.beam_search_exapand(paths, enc_outs, beam_size)
        # prune
        if rescorer is None:
            paths = sorted(paths, reverse=True)
        elif step in greedy_complete:
            paths = order_beam_after_greedy_complete(rescorer, new_paths, da_emb, i, enc_outs, seq2seq, max_pred_len,
                                                     cfg)
        else:
            paths = order_beam_acording_to_rescorer(get_identity_score_func(), new_paths, da_emb, i, {})
        paths = paths[:beam_size]

        if save_progress_file:
            save_progress_file.write("Step: {}\n".format(step))
            for path in paths:
                toks = [x for x in seq2seq.text_embedder.reverse_embedding(path[1]) if x != PAD_TOK]
                save_progress_file.write(" ".join(toks) + '\n')
            save_progress_file.write("\n")
        if all([p[1][-1] in end_tokens for p in paths]):
            break
    return paths


def run_beam_search_with_rescorer(scorer, beam_search_model: TGEN_Model, das, beam_size, cfg, only_rerank_final=False,
                                  save_final_beam_path='', greedy_complete=[],
                                  max_pred_len=60, save_progress_path=None, also_rerank_final=False):
    if save_progress_path is not None:
        save_progress_file = open(save_progress_path.format(beam_size), 'w+')
    else:
        save_progress_file = None
    da_embedder = beam_search_model.da_embedder
    text_embedder = beam_search_model.text_embedder

    results = []
    should_save_beams = save_final_beam_path and not os.path.exists(save_final_beam_path) and only_rerank_final
    should_load_beams = save_final_beam_path and os.path.exists(save_final_beam_path) and only_rerank_final
    load_final_beams = []
    final_beams = []
    if should_load_beams:
        print("Loading beams from", save_final_beam_path)
        load_final_beams = pickle.load((open(save_final_beam_path, "rb")))

    for i, da_emb in tqdm(list(enumerate(da_embedder.get_embeddings(das)))):
        if save_progress_file:
            save_progress_file.write("Test {}\n".format(i))
        inf_enc_out = beam_search_model.encoder_model.predict(np.array([da_emb]))
        enc_outs = inf_enc_out[0]
        enc_last_state = inf_enc_out[1:]
        paths = [(log(1.0), text_embedder.start_emb, enc_last_state)]

        if should_load_beams:
            paths = load_final_beams[i]
        else:
            paths = _run_beam_search_with_rescorer(
                i=i,
                da_emb=da_emb,
                paths=paths,
                enc_outs=enc_outs,
                beam_size=beam_size,
                max_pred_len=max_pred_len,
                seq2seq=beam_search_model,
                rescorer=scorer if not only_rerank_final else None,
                greedy_complete=greedy_complete,
                save_progress_file=save_progress_file,
                cfg=cfg
            )

        final_beams.append(paths)

        if only_rerank_final or also_rerank_final:
            paths = order_beam_acording_to_rescorer(scorer, paths, da_emb, i, cfg)
            if i == 0:
                print("First beam Score Distribution:")
                print([x[0] for x in paths])
                print("******************************")

        best_path = paths[0]
        pred_toks = text_embedder.reverse_embedding(best_path[1])
        results.append(pred_toks)

    if should_save_beams:
        print("Saving final beam states at ", save_final_beam_path)
        pickle.dump(final_beams, open(save_final_beam_path, "wb+"))

    return results


def get_best_from_beam_pairwise(beam, pair_wise_model, da_emb):
    path_scores = score_beams_pairwise(beam, pair_wise_model, da_emb)
    sorted_paths = sorted(path_scores, reverse=True, key=lambda x: x[0])
    return [x[1] for x in sorted_paths]

