import plac
import random
import pathlib
import cytoolz
import numpy
from keras.models import Sequential, model_from_json
from keras.layers import LSTM, Dense, Embedding, Bidirectional
from keras.layers import TimeDistributed
from keras.optimizers import Adam
import thinc.extra.datasets
from spacy.compat import pickle
import spacy


class SentimentAnalyser(object):
    @classmethod
    def load(cls, path, nlp, max_length=100):
        with (path / 'config.json').open() as file_:
            model = model_from_json(file_.read())
        with (path / 'model').open('rb') as file_:
            lstm_weights = pickle.load(file_)
        embeddings = get_embeddings(nlp.vocab)
        model.set_weights([embeddings] + lstm_weights)
        return cls(model, max_length=max_length)

    def __init__(self, model, max_length=100):
        self._model = model
        self.max_length = max_length

    def __call__(self, doc):
        X = get_features([doc], self.max_length)
        y = self._model.predict(X)
        self.set_sentiment(doc, y)

    def pipe(self, docs, batch_size=1000, n_threads=2):
        for minibatch in cytoolz.partition_all(batch_size, docs):
            minibatch = list(minibatch)
            sentences = []
            for doc in minibatch:
                sentences.extend(doc.sents)
            Xs = get_features(sentences, self.max_length)
            ys = self._model.predict(Xs)
            for sent, label in zip(sentences, ys):
                sent.doc.sentiment += label - 0.5
            for doc in minibatch:
                yield doc

    def set_sentiment(self, doc, y):
        doc.sentiment = float(y[0])
        # Sentiment has a native slot for a single float.
        # For arbitrary data storage, there's:
        # doc.user_data['my_data'] = y


def get_labelled_sentences(docs, doc_labels):
    """
    for every row in docs, we split it into sentences and add the correct label per sentence, rather than full doc
    """
    labels = []
    sentences = []
    for doc, y in zip(docs, doc_labels):
        for sent in doc.sents:
            sentences.append(sent)
            labels.append(y)
    return sentences, numpy.asarray(labels, dtype='int32')


def get_features(docs, max_length):
    """
    adds zero padding to get same length on every doc, if the vector is shorter it will fill zeros as padding
    :param docs:
    :param max_length:
    :return:
    """
    docs = list(docs)
    Xs = numpy.zeros((len(docs), max_length), dtype='int32')
    for i, doc in enumerate(docs):
        j = 0
        for token in doc:
            vector_id = token.vocab.vectors.find(key=token.orth)
            if vector_id >= 0:
                Xs[i, j] = vector_id
            else:
                Xs[i, j] = 0
            j += 1
            if j >= max_length:
                break
    return Xs


def train(train_texts, train_labels, dev_texts, dev_labels, lstm_shape, lstm_settings, batch_size=100,
          nb_epoch=5, by_sentence=True):
    print("Loading spaCy")
    nlp = spacy.load('en_vectors_web_lg')
    nlp.add_pipe(nlp.create_pipe('sentencizer'))
    embeddings = get_embeddings(nlp.vocab)
    # model = compile_lstm(embeddings, lstm_shape, lstm_settings)
    print("Parsing texts...")
    train_docs = list(nlp.pipe(train_texts))
    dev_docs = list(nlp.pipe(dev_texts))
    if by_sentence:
        train_docs, train_labels = get_labelled_sentences(train_docs, train_labels)
        dev_docs, dev_labels = get_labelled_sentences(dev_docs, dev_labels)

    train_X = get_features(train_docs, lstm_shape['max_length'])
    dev_X = get_features(dev_docs, lstm_shape['max_length'])
    model.fit(train_X, train_labels, validation_data=(dev_X, dev_labels), nb_epoch=nb_epoch, batch_size=batch_size)
    return model


def compile_lstm(embeddings, shape, settings):
    model = Sequential()
    model.add(Embedding(embeddings.shape[0], embeddings.shape[1], input_length=shape['max_length'], trainable=False, weights=[embeddings], mask_zero=True))
    model.add(TimeDistributed(Dense(shape['nr_hidden'], use_bias=False)))
    model.add(Bidirectional(LSTM(shape['nr_hidden'], recurrent_dropout=settings['dropout'], dropout=settings['dropout'])))
    model.add(Dense(shape['nr_class'], activation='sigmoid'))
    model.compile(optimizer=Adam(lr=settings['lr']), loss='binary_crossentropy', metrics=['accuracy'])
    return model


def get_embeddings(vocab):
    return vocab.vectors.data


def evaluate(model_dir, texts, labels, max_length=100):
    def create_pipeline(nlp):
        """
        This could be a lambda, but named functions are easier to read in Python.
        """
        return [nlp.tagger, nlp.parser, SentimentAnalyser.load(model_dir, nlp,
                                                               max_length=max_length)]

    nlp = spacy.load('en')
    nlp.pipeline = create_pipeline(nlp)

    correct = 0
    i = 0
    for doc in nlp.pipe(texts, batch_size=1000, n_threads=4):
        correct += bool(doc.sentiment >= 0.5) == bool(labels[i])
        i += 1
    return float(correct) / i


def read_data(data_dir, limit=0):
    examples = []
    for subdir, label in (('pos', 1), ('neg', 0)):
        for filename in (data_dir / subdir).iterdir():
            with filename.open() as file_:
                text = file_.read()
            examples.append((text, label))
    random.shuffle(examples)
    if limit >= 1:
        examples = examples[:limit]
    return zip(*examples)  # Unzips into two lists


@plac.annotations(
    train_dir=("Location of training file or directory"),
    dev_dir=("Location of development file or directory"),
    model_dir=("Location of output model directory",),
    is_runtime=("Demonstrate run-time usage", "flag", "r", bool),
    nr_hidden=("Number of hidden units", "option", "H", int),
    max_length=("Maximum sentence length", "option", "L", int),
    dropout=("Dropout", "option", "d", float),
    learn_rate=("Learn rate", "option", "e", float),
    nb_epoch=("Number of training epochs", "option", "i", int),
    batch_size=("Size of minibatches for training LSTM", "option", "b", int),
    nr_examples=("Limit to N examples", "option", "n", int)
)
def main(model_dir=None, train_dir=None, dev_dir=None,
         is_runtime=False,
         nr_hidden=64, max_length=100,  # Shape
         dropout=0.5, learn_rate=0.001,  # General NN config
         nb_epoch=5, batch_size=100, nr_examples=-1):  # Training params
    if model_dir is not None:
        model_dir = pathlib.Path(model_dir)
    if train_dir is None or dev_dir is None:
        imdb_data = thinc.extra.datasets.imdb()
    if is_runtime:
        if dev_dir is None:
            dev_texts, dev_labels = zip(*imdb_data[1])
        else:
            dev_texts, dev_labels = read_data(dev_dir)
        acc = evaluate(model_dir, dev_texts, dev_labels, max_length=max_length)
        print(acc)
    else:
        # imdb_date = tuple of (train_tuples, dev_tuples)
        # train/dev_tuples = (text, class_label)
        if train_dir is None:
            train_texts, train_labels = zip(*imdb_data[0])
        else:
            print("Read data")
            train_texts, train_labels = read_data(train_dir, limit=nr_examples)
        if dev_dir is None:
            dev_texts, dev_labels = zip(*imdb_data[1])
        else:
            dev_texts, dev_labels = read_data(dev_dir, imdb_data, limit=nr_examples)
        train_labels = numpy.asarray(train_labels, dtype='int32')
        dev_labels = numpy.asarray(dev_labels, dtype='int32')
        lstm = train(train_texts, train_labels, dev_texts, dev_labels,
                     {'nr_hidden': nr_hidden, 'max_length': max_length, 'nr_class': 1},
                     {'dropout': dropout, 'lr': learn_rate},
                     nb_epoch=nb_epoch, batch_size=batch_size, by_sentence=False)
        weights = lstm.get_weights()
        if model_dir is not None:
            with (model_dir / 'model').open('wb') as file_:
                pickle.dump(weights[1:], file_)
            with (model_dir / 'config.json').open('wb') as file_:
                file_.write(lstm.to_json())

# add in quora training data
from sklearn.model_selection import train_test_split
from helpers import get_data
df = get_data(unicoded=True)
df['text'] = df.question1 + ' ' + df.question2
train, test = train_test_split(df, train_size=0.95, random_state=49)
train_texts = train.text.values
train_labels = train.is_duplicate.values
train_labels = numpy.asarray(train_labels, dtype='int32')
dev_texts = test.text.values
dev_labels = test.is_duplicate.values
dev_labels = numpy.asarray(dev_labels, dtype='int32')

# init pipe
nlp = spacy.load('en_vectors_web_lg')
nlp.add_pipe(nlp.create_pipe('sentencizer'))

# save model
from helpers import save_model
save_model(lstm, 'models/spacy-lstm/')

# extend trained model
lstm.fit(train_X, train_labels, validation_data=(dev_X, dev_labels), nb_epoch=20, batch_size=1000)

# write training predictions
tmp_data = get_data(unicoded=True)
tmp_data['text'] = tmp_data.question1 + u' ' + tmp_data.question2
tmp_texts = tmp_data.text.values
tmp_texts = list(nlp.pipe(tmp_texts))
tmp_X = get_features(tmp_texts, 100)
tmp_preds = lstm.predict(tmp_X)
tmp_df = pd.DataFrame({"test_id": tmp_data.id.values, "is_duplicate": tmp_preds.ravel()})
tmp_df.to_csv("spacy_lstm_train_weights.csv", index=False)

# apply to test data
test_data = get_data(test=True, unicoded=True)
test_data['text'] = test_data.question1 + u' ' + test_data.question2
final_texts = test_data.text.values
final_texts = list(nlp.pipe(final_texts))
final_X = get_features(final_texts, 100)
final_preds = lstm.predict(final_X)
final_df = pd.DataFrame({"test_id": test_data.test_id.values, "is_duplicate": np.round(final_preds.ravel()).astype(int)})
final_df.to_csv("spacy_LSTM_5epochs.csv", index=False)


if __name__ == '__main__':
    plac.call(main)
