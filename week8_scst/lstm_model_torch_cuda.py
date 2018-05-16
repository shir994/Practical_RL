import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable

# Note: unlike official pytorch tutorial, this model doesn't process one sample at a time
# because it's slow on GPU.  instead it uses masks just like ye olde theano/tensorflow.
# it doesn't use torch.nn.utils.rnn.pack_paded_sequence because reasons.

class FullTranslationModel(nn.Module):
    def __init__(self, inp_voc, out_voc,
                 emb_size, hid_size,):
        super(self.__class__, self).__init__()
        self.inp_voc = inp_voc
        self.out_voc = out_voc

        self.emb_inp = nn.Embedding(len(inp_voc), emb_size)
        self.emb_out = nn.Embedding(len(out_voc), emb_size)
        
        self.enc_bi_LSTM = nn.LSTM(emb_size, hid_size, batch_first=True,
                               num_layers=1, bidirectional=True)
        self.enc_LSTM  = nn.LSTM(2 * hid_size, 2 * hid_size, batch_first=True,
                                  num_layers=1, bidirectional=False)
        
        
        self.dec_start_lower = nn.Linear(2 * hid_size, 2 * hid_size)
        self.dec_start_upper = nn.Linear(2 * hid_size, 2 * hid_size)
        
        self.dec_lower = nn.GRUCell(emb_size, 2 * hid_size)
        self.dec_upper = nn.GRUCell(2 * hid_size, 2 * hid_size)
        
        self.logits = nn.Linear(2 * hid_size, len(out_voc))

    def encode(self, inp, **flags):
        """
        Takes symbolic input sequence, computes initial state
        :param inp: a vector of input tokens  (Variable, int64, 1d)
        :return: a list of initial decoder state tensors
        """
        inp_emb = self.emb_inp(inp)
        
        enc_seq_lower, _ = self.enc_bi_LSTM(inp_emb)
        
        enc_seq_upper, _ = self.enc_LSTM(enc_seq_lower)
        
        
        #enc_seq, _ = self.enc0(inp_emb)

        # select last element w.r.t. mask
        end_index = infer_length(inp, self.inp_voc.eos_ix)
        end_index[end_index >= inp.shape[1]] = inp.shape[1] - 1
        
        enc_seq_lower_last = enc_seq_lower[range(0, enc_seq_lower.shape[0]), end_index.data.cuda(), :]
        enc_seq_upper_last = enc_seq_upper[range(0, enc_seq_upper.shape[0]), end_index.data.cuda(), :]

        dec_start_lower = self.dec_start_lower(enc_seq_lower_last)
        dec_start_upper = self.dec_start_upper(enc_seq_upper_last)
        return [dec_start_lower, dec_start_upper]

    def decode(self, prev_state, prev_tokens, **flags):
        """
        Takes previous decoder state and tokens, returns new state and logits
        :param prev_state: a list of previous decoder state tensors
        :param prev_tokens: previous output tokens, an int vector of [batch_size]
        :return: a list of next decoder state tensors, a tensor of logits [batch,n_tokens]
        """
        #[prev_dec] = prev_state
        prev_dec_lower, prev_dec_upper = prev_state
        
        prev_emb = self.emb_out(prev_tokens)
        
        
        #new_dec_state = self.dec0(prev_emb, prev_dec)
        new_dec_state_lower = self.dec_lower(prev_emb, prev_dec_lower)
        new_dec_state_upper = self.dec_upper(new_dec_state_lower, prev_dec_upper)
        
        
        output_logits = self.logits(new_dec_state_upper)

        return [new_dec_state_lower, new_dec_state_upper], output_logits

    def forward(self, inp, out, eps=1e-30, **flags):
        """
        Takes symbolic int32 matrices of hebrew words and their english translations.
        Computes the log-probabilities of all possible english characters given english prefices and hebrew word.
        :param inp: input sequence, int32 matrix of shape [batch,time]
        :param out: output sequence, int32 matrix of shape [batch,time]
        :return: log-probabilities of all possible english characters of shape [bath,time,n_tokens]

        Note: log-probabilities time axis is synchronized with out
        In other words, logp are probabilities of __current__ output at each tick, not the next one
        therefore you can get likelihood as logprobas * tf.one_hot(out,n_tokens)
        """
        batch_size = inp.shape[0]
        bos = Variable(torch.LongTensor([self.out_voc.bos_ix] * batch_size)).cuda()
        logits_seq = [torch.log(to_one_hot(bos, len(self.out_voc)) + eps)]

        hid_state = self.encode(inp, **flags)
        for x_t in out.transpose(0,1)[:-1]:
            hid_state, logits = self.decode(hid_state, x_t, **flags)
            logits_seq.append(logits)

        return F.log_softmax(torch.stack(logits_seq, dim=1), dim=-1)

    def translate(self, inp, greedy=False, max_len = None, eps = 1e-30, **flags):
        """
        takes symbolic int32 matrix of hebrew words, produces output tokens sampled
        from the model and output log-probabilities for all possible tokens at each tick.
        :param inp: input sequence, int32 matrix of shape [batch,time]
        :param greedy: if greedy, takes token with highest probablity at each tick.
            Otherwise samples proportionally to probability.
        :param max_len: max length of output, defaults to 2 * input length
        :return: output tokens int32[batch,time] and
                 log-probabilities of all tokens at each tick, [batch,time,n_tokens]
        """
        batch_size = inp.shape[0]
        bos = Variable(torch.LongTensor([self.out_voc.bos_ix] * batch_size)).cuda()
        mask = Variable(torch.ones(batch_size).type(torch.ByteTensor)).cuda()
        logits_seq = [torch.log(to_one_hot(bos, len(self.out_voc)) + eps)]
        out_seq = [bos]

        hid_state = self.encode(inp, **flags)
        while True:
            hid_state, logits = self.decode(hid_state, out_seq[-1], **flags)
            if greedy:
                _, y_t = torch.max(logits, dim=-1)
            else:
                probs = F.softmax(logits, dim=-1)
                y_t = torch.multinomial(probs, 1)[:, 0]

            logits_seq.append(logits)
            out_seq.append(y_t)
            mask &= y_t != self.out_voc.eos_ix

            if not mask.any(): break
            if max_len and len(out_seq) >= max_len: break

        return torch.stack(out_seq, 1), F.log_softmax(torch.stack(logits_seq, 1), dim=-1)



### Utility functions ###

def infer_mask(seq, eos_ix, batch_first=True, include_eos=True, type=torch.FloatTensor):
    """
    compute length given output indices and eos code
    :param seq: tf matrix [time,batch] if batch_first else [batch,time]
    :param eos_ix: integer index of end-of-sentence token
    :param include_eos: if True, the time-step where eos first occurs is has mask = 1
    :returns: lengths, int32 vector of shape [batch]
    """
    assert seq.dim() == 2
    is_eos = (seq == eos_ix).type(torch.FloatTensor)
    if include_eos:
        if batch_first:
            is_eos = torch.cat((is_eos[:,:1]*0, is_eos[:, :-1]), dim=1)
        else:
            is_eos = torch.cat((is_eos[:1,:]*0, is_eos[:-1, :]), dim=0)
    count_eos = torch.cumsum(is_eos, dim=1 if batch_first else 0)
    mask = count_eos == 0
    return mask.type(type).cuda()

def infer_length(seq, eos_ix, batch_first=True, include_eos=True, type=torch.LongTensor):
    """
    compute mask given output indices and eos code
    :param seq: tf matrix [time,batch] if time_major else [batch,time]
    :param eos_ix: integer index of end-of-sentence token
    :param include_eos: if True, the time-step where eos first occurs is has mask = 1
    :returns: mask, float32 matrix with '0's and '1's of same shape as seq
    """
    mask = infer_mask(seq, eos_ix, batch_first, include_eos, type)
    return torch.sum(mask, dim=1 if batch_first else 0)


def to_one_hot(y, n_dims=None):
    """ Take integer y (tensor or variable) with n dims and convert it to 1-hot representation with n+1 dims. """
    y_tensor = y.data.cpu() if isinstance(y, Variable) else y
    y_tensor = y_tensor.type(torch.LongTensor).view(-1, 1)
    n_dims = n_dims if n_dims is not None else int(torch.max(y_tensor)) + 1
    y_one_hot = torch.zeros(y_tensor.size()[0], n_dims).scatter_(1, y_tensor, 1)
    y_one_hot = y_one_hot.view(*y.shape, -1)
    return Variable(y_one_hot).cuda() if isinstance(y, Variable) else y_one_hot.cuda()
