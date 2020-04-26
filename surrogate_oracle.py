import argparse
import os
import sys
import msgpack
import numpy as np
import yaml
import matplotlib.pyplot as plt
from keras.utils import to_categorical

from utils import get_training_variables, START_TOK, PAD_TOK, END_TOK, get_multi_reference_training_variables, \
    get_final_beam, get_test_das, get_true_sents
from base_models import TGEN_Model, TrainableReranker
from e2e_metrics.metrics.pymteval import BLEUScore
from embedding_extractor import TokEmbeddingSeq2SeqExtractor, DAEmbeddingSeq2SeqExtractor
from reimplement_reinforce import run_beam_search_with_rescorer
from scorer_functions import get_oracle_score_func, get_greedy_decode_score_func, get_score_function

surrogte_train_dict = {}


def save_scores_dict(i):
    print("Saving surrogate_train_dict after {} iterations".format(i))
    with open(cfg["surrogate_train_data_path"], 'wb+') as fp:
        msgpack.dump(surrogte_train_dict, fp)


def get_scores_from_greedy_decode_dict(cfg, da_embedder, text_embedder, texts, das):
    global surrogte_train_dict
    models = TGEN_Model(da_embedder, text_embedder, cfg["tgen_seq2seq_config"])
    models.load_models()

    bleu_scorer = BLEUScore()
    texts = [[x for x in xs if x not in [START_TOK, END_TOK, PAD_TOK]] for xs in texts]
    final_scorer = get_oracle_score_func(bleu_scorer, texts, text_embedder, reverse=False)
    should_load_data = os.path.exists(cfg["surrogate_train_data_path"]) and cfg["load_surrogate_data"]

    models.populate_cache()

    if should_load_data:
        print("Loading Training data")
        surrogte_train_dict = msgpack.load(open(cfg["surrogate_train_data_path"], 'rb+'), use_list=False,
                                           strict_map_key=False)
    if not should_load_data or cfg["get_rest_surrogate_data"]:
        print("Creating Training data")
        scorer_func = get_greedy_decode_score_func(models, final_scorer=final_scorer,
                                                   max_length_out=text_embedder.length,
                                                   save_scores=surrogte_train_dict)
        start_point = cfg.get("surrogate_data_start_point", 0)
        preds = run_beam_search_with_rescorer(scorer=scorer_func,
                                              beam_search_model=models,
                                              das=das[start_point:],
                                              beam_size=10,
                                              only_rerank_final=True,
                                              save_final_beam_path=cfg.get('beam_save_path', None),
                                              callback_1000=save_scores_dict)

        print("Saving training data")
        save_scores_dict(-1)
        models.save_cache()

    text_seqs = []
    da_seqs = []
    scores = []
    log_probs = []
    print(len(surrogte_train_dict))
    for (da_emb, text_emb), (score, log_prob) in surrogte_train_dict.items():
        da_seqs.append(da_embedder.add_pad_to_embed(da_emb, to_start=True))
        text_seqs.append(text_embedder.add_pad_to_embed(text_emb, to_start=True))
        scores.append(score)
        log_probs.append(log_prob)

    text_seqs = np.array(text_seqs)
    da_seqs = np.array(da_seqs)
    scores = np.array(scores).reshape((-1, 1))
    log_probs = np.array(log_probs).reshape((-1, 1))

    # log probs need to be normalised
    print("Before: ", np.min(log_probs), np.ptp(log_probs))
    log_probs = (log_probs - np.min(log_probs)) / np.ptp(log_probs)
    print("After: ", np.min(log_probs), np.ptp(log_probs))
    if cfg['renormalise_scores']:
        orig_mean = scores.mean()
        orig_sd = scores.std()
        new_mean = 0.5
        new_sd = 0.4  # clip range is approx 0.45-> 0.9 (20% of data is clipped)
        new_scores = (scores - orig_mean) / orig_sd * new_sd + new_mean
        new_scores = new_scores.clip(0, 1)
        print("(μ,σ) = ({},{}) -> ({},{})".format(orig_mean, orig_sd, new_scores.mean(), new_scores.std()))
        scores = new_scores
    return text_seqs, da_seqs, scores, log_probs


def get_scores_ordered_beam(cfg, da_embedder, text_embedder):
    beam_size = cfg["beam_size"]
    models = TGEN_Model(da_embedder, text_embedder, cfg["tgen_seq2seq_config"])
    models.load_models()
    train_texts, train_das = get_multi_reference_training_variables()
    beam_save_path = 'output_files/saved_beams/train_vanilla_{}.txt'.format(beam_size)
    if cfg["reload_saved_beams"] or not os.path.exists(beam_save_path):
        print("Loading final beams")
        scorer = get_score_function('identity', cfg, models, None)
        run_beam_search_with_rescorer(scorer, models, das, beam_size, only_rerank_final=True,
                                      save_final_beam_path=beam_save_path)
    bleu = BLEUScore()
    final_beam = get_final_beam(beam_size, True)
    text_seqs = []
    da_seqs = []
    scores = []
    log_probs = []
    for beam, real_texts, da in zip(final_beam, train_texts, train_das):
        beam_scores = []
        for hyp, lp in beam:
            bleu.reset()
            bleu.append(hyp, real_texts)
            beam_scores.append((bleu.score(), hyp, lp))
        for i, (score, hyp, lp) in enumerate(sorted(beam_scores, reverse=True)):
            text_seqs.append(hyp)
            da_seqs.append(da)
            scores.append(to_categorical([i], num_classes=beam_size))
            log_probs.append(lp)

    text_seqs = np.array(text_embedder.get_embeddings(text_seqs, pad_from_end=False))
    da_seqs = np.array(da_embedder.get_embeddings(da_seqs))
    scores = np.array(scores).reshape((-1, beam_size))
    log_probs = np.array(log_probs).reshape((-1, 1))

    # log probs need to be normalised
    print("Before: ", np.min(log_probs), np.ptp(log_probs))
    log_probs = (log_probs - np.min(log_probs)) / np.ptp(log_probs)
    print("After: ", np.min(log_probs), np.ptp(log_probs))
    return text_seqs, da_seqs, scores, log_probs


parser = argparse.ArgumentParser()
parser.add_argument('config_path')
args = parser.parse_args()

cfg_path = args.config_path
print("Using config from: {}".format(cfg_path))
cfg = yaml.load(open(cfg_path, "r"))
texts, das = get_multi_reference_training_variables()
da_embedder = DAEmbeddingSeq2SeqExtractor(das)
# This is a very lazy move
texts_flat, _ = get_training_variables()
text_embedder = TokEmbeddingSeq2SeqExtractor(texts_flat)

print("Training")

reranker = TrainableReranker(da_embedder, text_embedder, cfg_path)
reranker.load_model()

if "train" in cfg and cfg["train"]:
    if cfg["train_data_type"] == 'default':
        text_seqs, da_seqs, scores, log_probs = \
            get_scores_from_greedy_decode_dict(cfg, da_embedder, text_embedder, texts, das)
    elif cfg["train_data_type"] == 'ordered_beams':
        text_seqs, da_seqs, scores, log_probs = \
            get_scores_ordered_beam(cfg, da_embedder, text_embedder)

    valid_size = cfg["valid_size"]
    reranker.train(text_seqs, da_seqs, scores, log_probs, cfg["epoch"], valid_size, cfg.get("min_passes", 5))

if "get_stats" in cfg and cfg["get_stats"]:
    test_das = get_test_das()
    test_texts = get_true_sents()
    # print("Loading final beams")
    # scorer = get_score_function('identity', cfg, models, None)
    # run_beam_search_with_rescorer(scorer, models, test_das, 3, only_rerank_final=False,
    #                               save_final_beam_path='output_files/saved_beams/vanilla_3.txt')

    test_da_embs = da_embedder.get_embeddings(test_das)
    final_beam = get_final_beam(cfg['beam_size'])
    beam_texts = [[text for text, _ in beam] for beam in final_beam]
    beam_tok_logprob = [[tp for _, tp in beam] for beam in final_beam]
    # test_text_embs = [text_embedder.get_embeddings(beam) for beam in beam_texts]
    bleu = BLEUScore()
    mapping = []
    order_correct_surrogate = 0
    order_correct_seq2seq = 0
    for texts, da_emb, tp_emb, true_texts in zip(beam_texts, test_da_embs, beam_tok_logprob, test_texts):
        text_seqs = np.array(text_embedder.get_embeddings(texts, pad_from_end=False))
        da_seqs = np.array([da_emb for _ in range(len(text_seqs))])
        tp_seqs = np.array(tp_emb).reshape(-1, 1)
        preds = reranker.predict_bleu_score(text_seqs, da_seqs, tp_seqs)
        beam_scores = []
        for i, (pred, text, tp) in enumerate(zip(preds, texts, tp_seqs)):
            bleu.reset()
            bleu.append(text, true_texts)
            real = bleu.score()
            mapping.append((pred[0], real))
            beam_scores.append((real, pred[0], i, tp[0]))
        best = sorted(beam_scores, reverse=True)[0][2]
        best_surrogate = sorted(beam_scores, key=lambda x: x[1])[0][2]
        best_seq2seq = sorted(beam_scores, key=lambda x: x[3], reverse=True)[0][2]
        if best == best_surrogate:
            order_correct_surrogate += 1
        if best == best_seq2seq:
            order_correct_seq2seq += 1
    print(len(beam_texts), order_correct_surrogate, order_correct_seq2seq)

    # print(mapping)
    preds = [x for x, _ in mapping]
    reals = [x for _, x in mapping]
    plt.scatter(reals, preds, alpha=0.1)
    plt.plot([0, 1], [0, 1], color='red')
    plt.xlabel("Real Score")
    plt.ylabel("Predicted")
    plt.show()
