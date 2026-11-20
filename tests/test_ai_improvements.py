"""Regression tests for vision decisions and the existing integration contracts.
Model accuracy on real faces is deliberately not claimed by these tests.
"""
import io
import threading
from unittest.mock import Mock, MagicMock

import cv2
import numpy as np
import pytest

from ai.expression_detection import EmotionDetector
from ai.output import concise_answer, spoken_text
from ai.llm import GroqAI
from services.sessions import SessionManager


@pytest.fixture
def emotion():
    d = EmotionDetector.__new__(EmotionDetector)
    d.labels = ['marah', 'jijik', 'takut', 'senang', 'sedih', 'kaget', 'netral']
    d.confidence_threshold, d.margin_threshold = .55, .15
    d.expressive_confidence, d.expressive_margin = .70, .20
    return d


def vector(label, score):
    labels = ['marah', 'jijik', 'takut', 'senang', 'sedih', 'kaget', 'netral']
    p = np.full(7, (1-score)/6)
    p[labels.index(label)] = score
    return p


def observation(d, label, score=.85, box=None):
    return [{**d._decision(vector(label, score)), 'box': box or [200, 100, 100, 100], 'quality_ok': True}]


@pytest.mark.parametrize('label', ['netral', 'senang', 'sedih'])
def test_three_supported_classes(emotion, label):
    result = emotion._decision(vector(label, .85))
    assert result['reliable'] and result['emotion'] == label
    assert EmotionDetector.summarize([result]) == f'Wajah terlihat {label}.'


@pytest.mark.parametrize('label', ['senang', 'sedih'])
def test_expressive_threshold_rejects_weak_result(emotion, label):
    result = emotion._decision(vector(label, .60))
    assert not result['reliable'] and result['emotion'] == 'belum yakin'


def test_uncertainty_is_not_neutral(emotion):
    assert emotion._decision(vector('marah', .95))['emotion'] == 'belum yakin'
    assert emotion._decision(np.full(7, 1/7))['emotion'] == 'belum yakin'
    assert emotion._decision(vector('netral', .60))['emotion'] == 'netral'


def test_burst_rejects_one_frame_emotional_flicker(emotion):
    emotion.detect_faces = Mock(side_effect=[observation(emotion, 'netral'),
        observation(emotion, 'netral'), observation(emotion, 'senang')])
    assert not emotion.detect_burst([1, 2, 3])[0]['reliable']


def test_burst_confirmed_current_track(emotion):
    emotion.detect_faces = Mock(side_effect=[observation(emotion, 'sedih', box=[198, 100, 100, 100]),
        observation(emotion, 'sedih'), observation(emotion, 'sedih', box=[202, 100, 100, 100])])
    result = emotion.detect_burst([1, 2, 3])[0]
    assert result['emotion'] == 'sedih' and result['temporal_verified']
    assert result['box'][0] == 202


def test_burst_reset_after_face_disappears_and_across_calls(emotion):
    emotion.detect_faces = Mock(side_effect=[observation(emotion, 'senang'), [], observation(emotion, 'senang')])
    assert not emotion.detect_burst([1, 2, 3])[0]['reliable']
    emotion.detect_faces = Mock(return_value=[])
    assert emotion.detect_burst([4, 5, 6]) == []


def test_burst_does_not_join_disjoint_faces(emotion):
    emotion.detect_faces = Mock(side_effect=[observation(emotion, 'senang', box=[0, 0, 50, 50]),
        observation(emotion, 'senang', box=[0, 0, 50, 50]), observation(emotion, 'senang')])
    assert not emotion.detect_burst([1, 2, 3])[0]['temporal_verified']


def test_spoken_output_strips_scores_and_limits_words():
    assert spoken_text('Rambu Kecepatan Maks. 40 km') == 'Rambu Kecepatan Maks. 40 km'
    assert concise_answer('Di depan ada kursi dengan confidence 94.2%.') == 'Di depan ada kursi.'
    assert concise_answer('Ada kursi. Ada kursi.') == 'Ada kursi.'
    text = ' '.join(f'kata{i}' for i in range(200))
    out = spoken_text(text)
    assert 'kata99' in out and 'kata100' not in out and 'lanjutan' in out


def test_groq_context_keeps_sign_without_model_scores(monkeypatch):
    monkeypatch.setattr('ai.llm.settings', Mock(groq_api_key='', groq_model='test'))
    g = GroqAI()
    g._generate = Mock(return_value='Ada rambu Dilarang Parkir.')
    assert g.ask_with_context('Rambu apa di depan?', sign_text='Terdeteksi: Dilarang Parkir.',
                              sign_confidence=.9, sign_reliable=True) == 'Ada rambu Dilarang Parkir.'
    prompt = g._generate.call_args.args[0]
    assert 'Dilarang Parkir' in prompt and 'confidence' not in prompt.lower().replace('deteksi rambu reliable', '')


def test_uncertain_sign_never_sent_to_groq(monkeypatch):
    monkeypatch.setattr('ai.llm.settings', Mock(groq_api_key='', groq_model='test'))
    g = GroqAI()
    g._generate = Mock()
    assert 'belum' in g.ask_with_context('Rambu apa di depan?', sign_text='ragu', sign_reliable=False)
    g._generate.assert_not_called()


def test_reading_text_questions_are_not_sign_questions():
    assert GroqAI.is_sign_question('Rambu apa di depan?')
    assert GroqAI.is_sign_question('lampu merah atau hijau?')
    assert not GroqAI.is_sign_question('Bacakan tulisan di papan itu')


def test_mongodb_ubidots_contracts_without_network(monkeypatch):
    from services.database import Database
    d = Database.__new__(Database)
    client = MagicMock()
    d._ensure_client = Mock(return_value=client)
    assert d.save_interaction({'response': 'Wajah terlihat netral.'})
    collection = client['SPIDER-SENSE']['Interaksi']
    payload = collection.insert_one.call_args.args[0]
    assert payload['response'] == 'Wajah terlihat netral.' and payload['timestamp']
    d.UBIDOTS_TOKEN, d.UBIDOTS_DEVICE_LABEL = 'unit-test-token', 'unit-test-device'
    post = Mock(return_value=Mock())
    monkeypatch.setattr('services.database.requests.post', post)
    assert d._send_ubidots('rabu')
    assert post.call_args.args[0].endswith('/unit-test-device/')
    assert post.call_args.kwargs['json']['rabu']['value'] == 1
    post.return_value.raise_for_status.assert_called_once()


def test_quick_api_expression_burst_contract(emotion, tmp_path, monkeypatch):
    from flask import Flask
    from services.quick_commands import register_quick_commands
    monkeypatch.setattr('services.quick_commands.BASE_DIR', tmp_path)
    d = Mock()
    d.detect_burst.return_value = observation(emotion, 'netral')
    d.summarize.return_value = 'Wajah terlihat netral.'
    services = {'expression': d, 'object': Mock(), 'sign': Mock(), 'scene': Mock()}
    app = Flask(__name__)
    register_quick_commands(app, services, SessionManager(), lambda: None, lambda: None)
    raw = cv2.imencode('.jpg', np.full((480, 640, 3), 128, np.uint8))[1].tobytes()
    data = {'mode': 'ekspresi', 'session_id': 'a', 'request_id': 'one',
            'image': [(io.BytesIO(raw), f'{i}.jpg') for i in range(3)]}
    response = app.test_client().post('/api/quick', data=data)
    assert response.status_code == 200 and response.json['result'] == 'Wajah terlihat netral.'
    assert len(d.detect_burst.call_args.args[0]) == 3
    assert not services['object'].mock_calls and not services['sign'].mock_calls


def test_pipeline_sign_mode_returns_sign_fields():
    from ai.pipeline import VisionPipeline
    boxes = [{'box': [1, 2, 3, 4], 'label': 'Polisi Tidur', 'confidence': .8}]
    sign = Mock(analyze=Mock(return_value={'text': 'Terdeteksi: Polisi Tidur.', 'confidence': .8,
                                           'reliable': True, 'boxes': boxes, 'labels': ['Polisi Tidur']}))
    result = VisionPipeline({'sign': sign}).run('rambu', [np.full((480, 640, 3), 128, np.uint8)])
    assert result['sign_text'] == 'Terdeteksi: Polisi Tidur.' and result['sign_boxes'] == boxes
    assert result['result'] == 'Terdeteksi: Polisi Tidur.'


def test_groq_completion_is_cleaned_before_tts(monkeypatch):
    monkeypatch.setattr('ai.llm.settings', Mock(groq_api_key='', groq_model='test', groq_max_output_tokens=220))
    g = GroqAI()
    g.available = True
    g.client = Mock()
    g.client.chat.completions.create.return_value.choices = [Mock(message=Mock(content='Ada kursi dengan confidence 94.2%. Ada kursi dengan confidence 94.2%.'))]
    assert g.ask('Apa di depan?') == 'Ada kursi.'
