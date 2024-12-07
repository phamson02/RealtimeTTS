from .base_engine import BaseEngine
from queue import Queue
import numpy as np
import torch
import sys
import os

class StyleTTSEngine(BaseEngine):
    def __init__(self, 
                 style_root: str, 
                 model_config_path: str, 
                 model_checkpoint_path: str, 
                 ref_audio_path: str,  # path to reference audio for style
                 device: str = 'cuda',
                 alpha: float = 0.3,
                 beta: float = 0.7,
                 diffusion_steps: int = 5,
                 embedding_scale: float = 1.0):
        """
        Initializes the StyleTTS engine with customizable parameters.
        
        Args:
            style_root (str): Path to the root directory of the StyleTTS repository.
                This is where all model-related files and dependencies are located.

            model_config_path (str): Path to the StyleTTS model configuration YAML file.
                This file defines model parameters such as architecture, layers, and training settings.

            model_checkpoint_path (str): Path to the pre-trained StyleTTS model checkpoint.
                The checkpoint file contains the learned weights of the model for inference.

            ref_audio_path (str): Path to a reference audio file for extracting style embedding.
                This file provides information about the desired speaking style (e.g., timbre, prosody)
                and is used to guide the synthesis.

            device (str): Device to run inference on. 
                Options:
                - 'cuda': Use a GPU for faster performance (requires a CUDA-compatible GPU).
                - 'cpu': Use the CPU (slower but does not require a GPU).

            alpha (float): Timbre blending factor (range: 0.0 to 1.0).
                - Controls the balance between the model's predicted timbre and the style extracted 
                  from the reference audio.
                - A higher alpha (closer to 1.0) means more reliance on the predicted timbre, resulting
                  in a more generic style determined by the model.
                - A lower alpha (closer to 0.0) means more reliance on the reference audio's timbre,
                  which can make the output closely match the style of the reference but might reduce generality.

            beta (float): Prosody blending factor (range: 0.0 to 1.0).
                - Controls the balance between the model's predicted prosody (intonation, rhythm, pitch) 
                  and the style extracted from the reference audio.
                - A higher beta (closer to 1.0) makes the output prosody more generic and less tied to 
                  the reference audio.
                - A lower beta (closer to 0.0) makes the output prosody more closely resemble the reference, 
                  which can lead to unique or expressive styles.

            diffusion_steps (int): Number of steps in the diffusion process.
                - Defines the granularity of refinement during the synthesis process.
                - A higher number of steps (e.g., 10 or 15) produces more detailed and refined audio 
                  but increases inference time.
                - A lower number of steps (e.g., 3 or 5) speeds up synthesis but may lead to less clear 
                  or stable audio.

            embedding_scale (float): Classifier-free guidance scale (range: typically 0.8 to 2.0).
                - This parameter amplifies the effect of the reference style on the synthesis.
                - A higher scale (e.g., 1.2 or 1.5) strengthens the alignment with the text and reference, 
                  potentially enhancing style adherence and expressiveness.
                - A very high scale might introduce artifacts or unnatural audio, so fine-tuning is recommended.
        """
        self.device = device if torch.cuda.is_available() else 'cpu'
        self.style_root = style_root.replace("\\", "/")
        self.model_config_path = model_config_path.replace("\\", "/")
        self.model_checkpoint_path = model_checkpoint_path.replace("\\", "/")
        self.ref_audio_path = ref_audio_path

        # Parameters for synthesis
        self.alpha = alpha
        self.beta = beta
        self.diffusion_steps = diffusion_steps
        self.embedding_scale = embedding_scale

        # Add the root directory to sys.path
        sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), self.style_root)))

        self.queue = Queue()
        self.load_model()
        self.compute_reference_style(self.ref_audio_path)
        self.post_init()

    def post_init(self):
        self.engine_name = "styletts"

    def get_stream_info(self):
        """
        Returns the PyAudio stream configuration:
          - Format: pyaudio.paInt16 (16-bit)
          - Channels: 1 (mono)
          - Sample Rate: 24000 Hz
        """
        import pyaudio
        return pyaudio.paInt16, 1, 24000

    def synthesize(self, text: str) -> bool:
        """
        Synthesizes text to audio stream using the loaded StyleTTS model.
        
        Args:
            text (str): Text to synthesize.
        """
        audio_waveform = self.inference(text,
                                        alpha=self.alpha,
                                        beta=self.beta,
                                        diffusion_steps=self.diffusion_steps,
                                        embedding_scale=self.embedding_scale)
        if audio_waveform is not None:
            audio_data = (audio_waveform * 32767).astype(np.int16).tobytes()
            self.queue.put(audio_data)
            return True
        else:
            return False

    def load_model(self):
        """
        Loads the StyleTTS model and necessary components.
        """
        import yaml
        import torch
        import torchaudio
        import librosa
        import nltk
        from munch import Munch
        from models import build_model, load_ASR_models, load_F0_models
        from utils import recursive_munch
        from text_utils import TextCleaner
        from Modules.diffusion.sampler import DiffusionSampler, ADPM2Sampler, KarrasSchedule
        import numpy as np
        import phonemizer
        from Utils.PLBERT.util import load_plbert

        nltk.download('punkt', quiet=True)

        self.textcleaner = TextCleaner()
        torch.manual_seed(0)
        torch.backends.cudnn.benchmark = False
        torch.backends.cudnn.deterministic = True
        np.random.seed(0)
        
        # Load model config
        print('Loading model config from %s' % self.model_config_path)
        config = yaml.safe_load(open(self.model_config_path, 'r'))

        # Load pretrained ASR model
        ASR_config = config.get('ASR_config', False)
        ASR_path = config.get('ASR_path', False)
        asr_corrected_config = os.path.join(self.style_root, ASR_config)
        asr_corrected_path = os.path.join(self.style_root, ASR_path)
        self.text_aligner = load_ASR_models(asr_corrected_path, asr_corrected_config)

        # Load pretrained F0 model
        F0_path = config.get('F0_path', False)
        f0_path_corrected = os.path.join(self.style_root, F0_path)
        self.pitch_extractor = load_F0_models(f0_path_corrected)

        # Load BERT model
        BERT_path = config.get('PLBERT_dir', False)
        bert_path_corrected = os.path.join(self.style_root, BERT_path)
        self.plbert = load_plbert(bert_path_corrected)

        # Build model
        model_params = recursive_munch(config['model_params'])
        self.model_params = model_params
        self.model = build_model(model_params, self.text_aligner, self.pitch_extractor, self.plbert)
        _ = [self.model[key].eval() for key in self.model]
        _ = [self.model[key].to(self.device) for key in self.model]

        # Load model checkpoint
        params_whole = torch.load(self.model_checkpoint_path, map_location='cpu')
        params = params_whole['net']
        for key in self.model:
            if key in params:
                print('%s loaded' % key)
                try:
                    self.model[key].load_state_dict(params[key])
                except:
                    from collections import OrderedDict
                    state_dict = params[key]
                    new_state_dict = OrderedDict()
                    for k, v in state_dict.items():
                        name = k[7:]  # remove `module.`
                        new_state_dict[name] = v
                    self.model[key].load_state_dict(new_state_dict, strict=False)
        _ = [self.model[key].eval() for key in self.model]

        # Initialize mel spectrogram
        self.to_mel = torchaudio.transforms.MelSpectrogram(
            n_mels=80, n_fft=2048, win_length=1200, hop_length=300).to(self.device)
        self.mean, self.std = -4, 4

        # Initialize phonemizer
        self.global_phonemizer = phonemizer.backend.EspeakBackend(language='en-us', 
                                                                 preserve_punctuation=True,
                                                                 with_stress=True)

        # Initialize diffusion sampler
        self.sampler = DiffusionSampler(
            self.model.diffusion.diffusion,
            sampler=ADPM2Sampler(),
            sigma_schedule=KarrasSchedule(sigma_min=0.0001, sigma_max=3.0, rho=9.0),
            clamp=False
        )

        self.sample_rate = 24000

    def compute_reference_style(self, path):
        """
        Compute the style embedding from a reference audio.
        """
        import librosa
        import torch
        wave, sr = librosa.load(path, sr=24000)
        audio, _ = librosa.effects.trim(wave, top_db=30)
        if sr != 24000:
            audio = librosa.resample(audio, sr, 24000)
        wave_tensor = torch.from_numpy(audio).float().to(self.device)
        mel_tensor = self.to_mel(wave_tensor.unsqueeze(0))
        mel_tensor = (torch.log(1e-5 + mel_tensor) - self.mean) / self.std
        with torch.no_grad():
            ref_s = self.model.style_encoder(mel_tensor.unsqueeze(1))
            ref_p = self.model.predictor_encoder(mel_tensor.unsqueeze(1))
        self.ref_s = torch.cat([ref_s, ref_p], dim=1)

    def length_to_mask(self, lengths):
        mask = torch.arange(lengths.max()).unsqueeze(0).expand(lengths.shape[0], -1).type_as(lengths)
        mask = torch.gt(mask+1, lengths.unsqueeze(1))
        return mask

    def inference(self, text: str, 
                  alpha: float = 0.3, 
                  beta: float = 0.7, 
                  diffusion_steps: int = 5, 
                  embedding_scale: float = 1.0) -> np.ndarray:
        """
        Run inference with given parameters and return audio waveform.
        
        Args:
            text (str): Text to synthesize.
            alpha (float): Timbre blending factor.
            beta (float): Prosody blending factor.
            diffusion_steps (int): Number of diffusion steps.
            embedding_scale (float): Classifier-free guidance scale.

        Returns:
            numpy.ndarray: The synthesized audio waveform.
        """
        import torch
        from nltk.tokenize import word_tokenize

        text = text.strip()
        ps = self.global_phonemizer.phonemize([text])
        ps = word_tokenize(ps[0])
        ps = ' '.join(ps)
        tokens = self.textcleaner(ps)
        tokens.insert(0, 0)
        tokens = torch.LongTensor(tokens).to(self.device).unsqueeze(0)

        with torch.no_grad():
            input_lengths = torch.LongTensor([tokens.shape[-1]]).to(self.device)
            text_mask = self.length_to_mask(input_lengths).to(self.device)

            t_en = self.model.text_encoder(tokens, input_lengths, text_mask)
            bert_dur = self.model.bert(tokens, attention_mask=(~text_mask).int())
            d_en = self.model.bert_encoder(bert_dur).transpose(-1, -2)

            noise = torch.randn(1, 256).unsqueeze(1).to(self.device)
            s_pred = self.sampler(
                noise=noise,
                embedding=bert_dur,
                embedding_scale=embedding_scale,
                features=self.ref_s,
                num_steps=diffusion_steps
            ).squeeze(1)

            s = s_pred[:, 128:]
            ref = s_pred[:, :128]

            # Blend style with reference
            ref = alpha * ref + (1 - alpha) * self.ref_s[:, :128]
            s = beta * s + (1 - beta) * self.ref_s[:, 128:]

            d = self.model.predictor.text_encoder(d_en, s, input_lengths, text_mask)
            x, _ = self.model.predictor.lstm(d)
            duration = self.model.predictor.duration_proj(x)
            duration = torch.sigmoid(duration).sum(axis=-1)
            pred_dur = torch.round(duration.squeeze()).clamp(min=1)

            pred_aln_trg = torch.zeros(input_lengths, int(pred_dur.sum().item())).to(self.device)
            c_frame = 0
            for i in range(pred_aln_trg.size(0)):
                pred_aln_trg[i, c_frame:c_frame + int(pred_dur[i].item())] = 1
                c_frame += int(pred_dur[i].item())

            en = (d.transpose(-1, -2) @ pred_aln_trg.unsqueeze(0))
            if self.model_params.decoder.type == "hifigan":
                asr_new = torch.zeros_like(en)
                asr_new[:, :, 0] = en[:, :, 0]
                asr_new[:, :, 1:] = en[:, :, 0:-1]
                en = asr_new

            F0_pred, N_pred = self.model.predictor.F0Ntrain(en, s)

            asr = (t_en @ pred_aln_trg.unsqueeze(0))
            if self.model_params.decoder.type == "hifigan":
                asr_new = torch.zeros_like(asr)
                asr_new[:, :, 0] = asr[:, :, 0]
                asr_new[:, :, 1:] = asr[:, :, 0:-1]
                asr = asr_new

            out = self.model.decoder(asr, F0_pred, N_pred, ref.squeeze().unsqueeze(0))
            waveform = out.squeeze().cpu().numpy()

        if waveform.shape[-1] > 50:
            waveform = waveform[..., :-50]

        return waveform