import gc
import os
import traceback
from typing import List, Literal, Optional, Union

import numpy as np
import torch
from diffusers import AutoencoderTiny, StableDiffusionPipeline
from PIL import Image
from polygraphy import cuda

from streamdiffusion import StreamDiffusion
from streamdiffusion.image_utils import postprocess_image

torch.set_grad_enabled(False)
torch.backends.cuda.matmul.allow_tf32 = True
torch.backends.cudnn.allow_tf32 = True


class StreamDiffusionWrapper:
    def __init__(
        self,
        model_id: str,
        t_index_list: List[int],
        mode: Literal["img2img", "txt2img"] = "img2img",
        output_type: Literal["pil", "pt", "np", "latent"] = "pil",
        lcm_lora_id: Optional[str] = None,
        vae_id: Optional[str] = None,
        device: Literal["cpu", "cuda"] = "cuda",
        dtype: torch.dtype = torch.float16,
        frame_buffer_size: int = 1,
        width: int = 512,
        height: int = 512,
        warmup: int = 10,
        acceleration: Literal["none", "xformers", "sfast", "tensorrt"] = "tensorrt",
        is_drawing: bool = True,
        device_ids: Optional[List[int]] = None,
        use_lcm_lora: bool = True,
        use_tiny_vae: bool = True,
        enable_similar_image_filter: bool = False,
        similar_image_filter_threshold: float = 0.98,
        use_denoising_batch: bool = True,
        cfg_type: Literal["none", "full", "self", "initialize"] = "self",
    ):
        if mode == "txt2img":
            if cfg_type != "none":
                raise ValueError(f"txt2img mode accepts only cfg_type = 'none', but got {cfg_type}")
            if use_denoising_batch and frame_buffer_size > 1:
                raise ValueError("txt2img mode cannot use denoising batch with frame_buffer_size > 1.")

        self.sd_turbo = "turbo" in model_id
        self.device = device
        self.dtype = dtype
        self.width = width
        self.height = height
        self.mode = mode
        self.output_type = output_type
        self.frame_buffer_size = frame_buffer_size
        self.batch_size = len(t_index_list) * frame_buffer_size if use_denoising_batch else frame_buffer_size

        self.use_denoising_batch = use_denoising_batch

        self.stream = self._load_model(
            model_id=model_id,
            lcm_lora_id=lcm_lora_id,
            vae_id=vae_id,
            t_index_list=t_index_list,
            acceleration=acceleration,
            warmup=warmup,
            is_drawing=is_drawing,
            use_lcm_lora=use_lcm_lora,
            use_tiny_vae=use_tiny_vae,
            cfg_type=cfg_type,
        )

        if device_ids is not None:
            self.stream.unet = torch.nn.DataParallel(self.stream.unet, device_ids=device_ids)

        if enable_similar_image_filter:
            self.stream.enable_similar_image_filter(similar_image_filter_threshold)

    def prepare(
        self,
        prompt: str,
        negative_prompt: str = "",
        num_inference_steps: int = 50,
        guidance_scale: float = 1.2,
        delta: float = 1.0,
    ) -> None:
        """
        Prepares the model for inference.

        Parameters
        ----------
        prompt : str
            The prompt to generate images from.
        num_inference_steps : int, optional
            The number of inference steps to perform, by default 50.
        """
        self.stream.prepare(
            prompt,
            negative_prompt,
            num_inference_steps=num_inference_steps,
            guidance_scale=guidance_scale,
            delta=delta,
        )

    def __call__(
        self,
        image: Optional[Union[str, Image.Image, torch.Tensor]] = None,
    ) -> Union[Image.Image, List[Image.Image]]:
        """
        Performs img2img or txt2img based on the mode.

        Parameters
        ----------
        image : Optional[Union[str, Image.Image, torch.Tensor]]
            The image to generate from.

        Returns
        -------
        Union[Image.Image, List[Image.Image]]
            The generated image.
        """
        if self.mode == "img2img":
            return self.img2img(image)
        else:
            return self.txt2img()

    def txt2img(self) -> Union[Image.Image, List[Image.Image], torch.Tensor, np.ndarray]:
        """
        Performs txt2img.

        Returns
        -------
        Union[Image.Image, List[Image.Image]]
            The generated image.
        """
        if self.sd_turbo:
            image_tensor = self.stream.txt2img_sd_turbo(self.batch_size)
        else:
            image_tensor = self.stream.txt2img(self.frame_buffer_size)
        return self.postprocess_image(image_tensor, output_type=self.output_type)

    def img2img(self, image: Union[str, Image.Image, torch.Tensor]) -> Union[Image.Image, List[Image.Image], torch.Tensor, np.ndarray]:
        """
        Performs img2img.

        Parameters
        ----------
        image : Union[str, Image.Image, torch.Tensor]
            The image to generate from.

        Returns
        -------
        Image.Image
            The generated image.
        """
        if isinstance(image, str) or isinstance(image, Image.Image):
            image = self.preprocess_image(image)

        image_tensor = self.stream(image)
        return self.postprocess_image(image_tensor, output_type=self.output_type)

    def preprocess_image(self, image: Union[str, Image.Image]) -> torch.Tensor:
        """
        Preprocesses the image.

        Parameters
        ----------
        image : Union[str, Image.Image, torch.Tensor]
            The image to preprocess.

        Returns
        -------
        torch.Tensor
            The preprocessed image.
        """
        if isinstance(image, str):
            image = Image.open(image).convert("RGB").resize((self.width, self.height))
        if isinstance(image, Image.Image):
            image = image.convert("RGB").resize((self.width, self.height))

        return self.stream.image_processor.preprocess(image, self.height, self.width).to(device=self.device, dtype=self.dtype)

    def postprocess_image(
        self, image_tensor: torch.Tensor, output_type: str = "pil"
    ) -> Union[Image.Image, List[Image.Image], torch.Tensor, np.ndarray]:
        """
        Postprocesses the image.

        Parameters
        ----------
        image_tensor : torch.Tensor
            The image tensor to postprocess.

        Returns
        -------
        Union[Image.Image, List[Image.Image]]
            The postprocessed image.
        """
        if self.frame_buffer_size > 1:
            return postprocess_image(image_tensor.cpu(), output_type=output_type)
        else:
            return postprocess_image(image_tensor.cpu(), output_type=output_type)[0]

    def _load_model(
        self,
        model_id: str,
        t_index_list: List[int],
        lcm_lora_id: Optional[str] = None,
        vae_id: Optional[str] = None,
        acceleration: Literal["none", "sfast", "tensorrt"] = "tensorrt",
        is_drawing: bool = True,
        warmup: int = 10,
        use_lcm_lora: bool = True,
        use_tiny_vae: bool = True,
        cfg_type: Literal["none", "full", "self", "initialize"] = "self",
    ):
        """
        Loads the model.

        This method does the following:

        1. Loads the model from the model_id.
        2. Loads and fuses the LCM-LoRA model from the lcm_lora_id if needed.
        3. Loads the VAE model from the vae_id if needed.
        4. Enables acceleration if needed.
        5. Prepares the model for inference.
        6. Warms up the model.

        Parameters
        ----------
        model_id : str
            The model id to load.
        t_index_list : List[int]
            The t_index_list to use for inference.
        lcm_lora_id : Optional[str], optional
            The lcm_lora_id to load, by default None.
        vae_id : Optional[str], optional
            The vae_id to load, by default None.
        acceleration : Literal["none", "xfomers", "sfast", "tensorrt"], optional
            The acceleration method to use, by default "tensorrt".
        warmup : int, optional
            The number of warmup steps to perform, by default 10.
        is_drawing : bool, optional
            Whether to draw the image or not, by default True.
        use_lcm_lora : bool, optional
            Whether to use LCM-LoRA or not, by default True.
        use_tiny_vae : bool, optional
            Whether to use TinyVAE or not, by default True.
        """

        try:  # Load from local directory
            pipe: StableDiffusionPipeline = StableDiffusionPipeline.from_pretrained(
                model_id,
            ).to(device=self.device, dtype=self.dtype)

        except ValueError:  # Load from huggingface
            pipe: StableDiffusionPipeline = StableDiffusionPipeline.from_single_file(model_id).to(device=self.device, dtype=self.dtype)
        except Exception:  # No model found
            traceback.print_exc()
            print("Model load has failed. Doesn't exist.")
            exit()

        stream = StreamDiffusion(
            pipe=pipe,
            t_index_list=t_index_list,
            torch_dtype=self.dtype,
            width=self.width,
            height=self.height,
            is_drawing=is_drawing,
            frame_buffer_size=self.frame_buffer_size,
            use_denoising_batch=self.use_denoising_batch,
            cfg_type=cfg_type,
        )
        if not self.sd_turbo:
            if use_lcm_lora:
                if lcm_lora_id is not None:
                    stream.load_lcm_lora(pretrained_model_name_or_path_or_dict=lcm_lora_id)
                else:
                    stream.load_lcm_lora()
                stream.fuse_lora()

        if use_tiny_vae:
            if vae_id is not None:
                stream.vae = AutoencoderTiny.from_pretrained(vae_id).to(device=pipe.device, dtype=pipe.dtype)
            else:
                stream.vae = AutoencoderTiny.from_pretrained("madebyollin/taesd").to(device=pipe.device, dtype=pipe.dtype)

        try:
            if acceleration == "xformers":
                stream.pipe.enable_xformers_memory_efficient_attention()
            if acceleration == "tensorrt":
                from streamdiffusion.acceleration.tensorrt import TorchVAEEncoder, compile_unet, compile_vae_decoder, compile_vae_encoder
                from streamdiffusion.acceleration.tensorrt.engine import AutoencoderKLEngine, UNet2DConditionModelEngine
                from streamdiffusion.acceleration.tensorrt.models import VAE, UNet, VAEEncoder

                def create_prefix(
                    max_batch_size: int,
                    min_batch_size: int,
                ):
                    return f"{model_id}--lcm_lora-{use_tiny_vae}--tiny_vae-{use_lcm_lora}--max_batch-{max_batch_size}--min_batch-{min_batch_size}--mode-{self.mode}"

                engine_dir = os.path.join("engines")
                unet_path = os.path.join(
                    engine_dir,
                    create_prefix(stream.trt_unet_batch_size, stream.trt_unet_batch_size),
                    "unet.engine",
                )
                vae_encoder_path = os.path.join(
                    engine_dir,
                    create_prefix(
                        self.batch_size if self.mode == "txt2img" else stream.frame_bff_size,
                        self.batch_size if self.mode == "txt2img" else stream.frame_bff_size,
                    ),
                    "vae_encoder.engine",
                )
                vae_decoder_path = os.path.join(
                    engine_dir,
                    create_prefix(
                        self.batch_size if self.mode == "txt2img" else stream.frame_bff_size,
                        self.batch_size if self.mode == "txt2img" else stream.frame_bff_size,
                    ),
                    "vae_decoder.engine",
                )

                if not os.path.exists(unet_path):
                    os.makedirs(os.path.dirname(unet_path), exist_ok=True)
                    unet_model = UNet(
                        fp16=True,
                        device=stream.device,
                        max_batch_size=stream.trt_unet_batch_size,
                        min_batch_size=stream.trt_unet_batch_size,
                        embedding_dim=stream.text_encoder.config.hidden_size,
                        unet_dim=stream.unet.config.in_channels,
                    )
                    compile_unet(
                        stream.unet,
                        unet_model,
                        unet_path + ".onnx",
                        unet_path + ".opt.onnx",
                        unet_path,
                        opt_batch_size=stream.trt_unet_batch_size,
                    )

                if not os.path.exists(vae_decoder_path):
                    os.makedirs(os.path.dirname(vae_decoder_path), exist_ok=True)
                    stream.vae.forward = stream.vae.decode
                    vae_decoder_model = VAE(
                        device=stream.device,
                        max_batch_size=self.batch_size if self.mode == "txt2img" else stream.frame_bff_size,
                        min_batch_size=self.batch_size if self.mode == "txt2img" else stream.frame_bff_size,
                    )
                    compile_vae_decoder(
                        stream.vae,
                        vae_decoder_model,
                        vae_decoder_path + ".onnx",
                        vae_decoder_path + ".opt.onnx",
                        vae_decoder_path,
                        opt_batch_size=self.batch_size if self.mode == "txt2img" else stream.frame_bff_size,
                    )
                    delattr(stream.vae, "forward")

                if not os.path.exists(vae_encoder_path):
                    os.makedirs(os.path.dirname(vae_encoder_path), exist_ok=True)
                    vae_encoder = TorchVAEEncoder(stream.vae).to(torch.device("cuda"))
                    vae_encoder_model = VAEEncoder(
                        device=stream.device,
                        max_batch_size=self.batch_size if self.mode == "txt2img" else stream.frame_bff_size,
                        min_batch_size=self.batch_size if self.mode == "txt2img" else stream.frame_bff_size,
                    )
                    compile_vae_encoder(
                        vae_encoder,
                        vae_encoder_model,
                        vae_encoder_path + ".onnx",
                        vae_encoder_path + ".opt.onnx",
                        vae_encoder_path,
                        opt_batch_size=self.batch_size if self.mode == "txt2img" else stream.frame_bff_size,
                    )

                cuda_steram = cuda.Stream()

                vae_config = stream.vae.config
                vae_dtype = stream.vae.dtype

                stream.unet = UNet2DConditionModelEngine(unet_path, cuda_steram, use_cuda_graph=False)
                stream.vae = AutoencoderKLEngine(
                    vae_encoder_path,
                    vae_decoder_path,
                    cuda_steram,
                    stream.pipe.vae_scale_factor,
                    use_cuda_graph=False,
                )
                setattr(stream.vae, "config", vae_config)
                setattr(stream.vae, "dtype", vae_dtype)

                gc.collect()
                torch.cuda.empty_cache()

                print("TensorRT acceleration enabled.")
            if acceleration == "sfast":
                from streamdiffusion.acceleration.sfast import accelerate_with_stable_fast

                stream = accelerate_with_stable_fast(stream)
                print("StableFast acceleration enabled.")
        except Exception:
            traceback.print_exc()
            print("Acceleration has failed. Falling back to normal mode.")

        stream.prepare(
            "",
            "",
            num_inference_steps=50,
            guidance_scale=1.1 if stream.cfg_type in ["full", "self", "initialize"] else 1.0,
            generator=torch.manual_seed(2),
        )

        return stream
