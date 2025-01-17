import os
import json
import pickle
import torch
from collections import defaultdict
from torch.utils.data import Dataset

import numpy as np

class SpeakerDataset(Dataset):
    def __init__(self, split, data_dir, chain_file, utterances_file, orig_ref_file,
                 vectors_file, subset_size):

        self.data_dir = data_dir
        self.split = split

        self.max_len = 0

        # Load a PhotoBook utterance chain dataset
        with open(os.path.join(self.data_dir, chain_file), 'r') as file:
            self.chains = json.load(file)

        # Load an underlying PhotoBook dialogue utterance dataset
        with open(os.path.join(self.data_dir, utterances_file), 'rb') as file:
            self.utterances = pickle.load(file)

        # Original reference sentences without unks
        with open(os.path.join(self.data_dir, orig_ref_file), 'rb') as file:
            self.text_refs = pickle.load(file)

        # Load pre-defined image features
        with open(os.path.join(data_dir, vectors_file), 'r') as file:
            self.image_features = json.load(file)

        self.img_dim = 2048
        self.img_count = 6  # images in the context

        self.data = dict()

        self.img2chain = defaultdict(dict)

        for chain in self.chains:

            self.img2chain[chain['target']][chain['game_id']] = chain['utterances']

        if subset_size == -1:
            self.subset_size = len(self.chains)
        else:
            self.subset_size = subset_size

        print('processing',self.split)

        # every utterance in every chain, along with the relevant history
        for chain in self.chains[:self.subset_size]:

            chain_utterances = chain['utterances']
            game_id = chain['game_id']

            for s in range(len(chain_utterances)):

                # this is the expected target generation
                utterance_id = tuple(chain_utterances[s])  # utterance_id = (game_id, round_nr, messsage_nr, img_id)
                round_nr = utterance_id[1]
                message_nr = utterance_id[2]

                # prev utterance in the chain
                for cu in range(len(chain['utterances'])):

                    if chain['utterances'][cu] == list(utterance_id):
                        if cu == 0:
                            previous_utterance = []
                        else:
                            prev_id = chain['utterances'][cu - 1]
                            previous_utterance = self.utterances[tuple(prev_id)]['utterance']

                        break

                # linguistic histories for images in the context
                # HISTORY before the expected generation (could be after the encoded history)
                prev_chains = defaultdict(list)
                prev_lengths = defaultdict(int)

                cur_utterance_obj = self.utterances[utterance_id]
                cur_utterance_text_ids= cur_utterance_obj['utterance']

                orig_target = self.text_refs[utterance_id]['utterance']
                orig_target = ' '.join(orig_target)

                length = cur_utterance_obj['length']

                if length > self.max_len:
                    self.max_len = length

                assert len(cur_utterance_text_ids) != 2
                # already had added sos eos into length and IDS version

                images = cur_utterance_obj['image_set']
                target = cur_utterance_obj['target']  # index of correct img

                target_image = images[target[0]]

                images = list(np.random.permutation(images))
                target = [images.index(target_image)]

                context_separate = torch.zeros(self.img_count, self.img_dim)

                im_counter = 0

                reference_chain = []

                for im in images:

                    context_separate[im_counter] = torch.tensor(self.image_features[im])

                    if im == images[target[0]]:
                        target_img_feats = context_separate[im_counter]
                        ref_chain = self.img2chain[im][game_id]

                        for rc in ref_chain:
                            rc_tuple = (rc[0], rc[1], rc[2], im)
                            reference_chain.append(' '.join(self.text_refs[rc_tuple]['utterance']))

                    im_counter += 1

                    if game_id in self.img2chain[im]:  # was there a linguistic chain for this image in this game
                        temp_chain = self.img2chain[im][game_id]

                        hist_utterances = []

                        for t in range(len(temp_chain)):

                            _, t_round, t_message, _ = temp_chain[t] #(game_id, round_nr, messsage_nr, img_id)

                            if t_round < round_nr:
                                hist_utterances.append((game_id, t_round, t_message))

                            elif t_round == round_nr:

                                if t_message < message_nr:
                                    hist_utterances.append((game_id, t_round, t_message))

                        if len(hist_utterances) > 0:

                            # ONLY THE MOST RECENT history
                            for hu in [hist_utterances[-1]]:
                                hu_tuple = (hu[0], hu[1], hu[2], im)
                                prev_chains[im].extend(self.utterances[hu_tuple]['utterance'])

                        else:
                            # no prev reference to that image
                            prev_chains[im] = []

                    else:
                        # image is in the game but never referred to
                        prev_chains[im] = []

                    prev_lengths[im] = len(prev_chains[im])

                # ALWAYS 6 IMAGES IN THE CONTEXT

                context_concat = context_separate.reshape(self.img_count * self.img_dim)

                self.data[len(self.data)] = {'utterance': cur_utterance_text_ids,
                                             'orig_utterance': orig_target,  # without unk, eos, sos, pad
                                             'image_set': images,
                                             'concat_context': context_concat,
                                             'separate_images': context_separate,
                                             'prev_utterance': previous_utterance,
                                             'prev_length': len(previous_utterance),
                                             'target':target,
                                             'target_img_feats': target_img_feats,
                                             'length': length,
                                             'prev_histories': prev_chains,
                                             'prev_history_lengths': prev_lengths,
                                             'reference_chain': reference_chain
                                             }

    def __len__(self):
        return len(self.data)

    def __getitem__(self, index):
        return self.data[index]

    @staticmethod
    def get_collate_fn(device, SOS, EOS, NOHS):

        def collate_fn(data):

            max_utt_length = max(d['length'] for d in data)
            max_prevutt_length = max([d['prev_length'] for d in data])

            batch = defaultdict(list)

            for sample in data:

                for key in data[0].keys():

                    if key == 'utterance':

                        padded = sample[key] + [0] * (max_utt_length - sample['length'])

                        # print('utt', padded)

                    elif key == 'prev_utterance':

                        if len(sample[key]) == 0:
                            # OTHERWISE pack_padded wouldn't work
                            padded = [NOHS] + [0] * (max_prevutt_length - 1) # SPECIAL TOKEN FOR NO HIST

                        else:
                            padded = sample[key] + [0] * (max_prevutt_length - len(sample[key]))

                        # print('prevutt', padded)

                    elif key == 'prev_length':

                        if sample[key] == 0:
                            # wouldn't work in pack_padded
                            padded = 1

                        else:
                            padded = sample[key]


                    elif key == 'image_set':

                        padded = [int(img) for img in sample['image_set']]

                        # print('img', padded)

                    elif key == 'prev_histories':

                        padded = sample['prev_histories']

                    else:
                        padded = sample[key]

                    batch[key].append(padded)

            for key in batch.keys():
                # print(key)

                if key in ['separate_images', 'concat_context', 'target_img_feats']:
                    batch[key] = torch.stack(batch[key]).to(device)

                elif key in ['utterance', 'prev_utterance', 'target', 'length', 'prev_length']:
                    batch[key] = torch.Tensor(batch[key]).long().to(device)

                    # for instance targets can be long and sent to device immediately

            return batch

        return collate_fn

