"""modified from https://github.com/wkentaro/labelme/blob/main/labelme/ai/models/segment_anything.py"""
import collections
import threading

import imgviz
import numpy as np
import onnxruntime
import PIL.Image
import skimage.measure

from labelme.logger import logger


class SegmentAnythingModel:
    def __init__(self, name, encoder_path, decoder_path):
        self.name = name

        self._image_size = 1024

        self._encoder_session = onnxruntime.InferenceSession(encoder_path)
        self._decoder_session = onnxruntime.InferenceSession(decoder_path)

        self._lock = threading.Lock()
        self._image_embedding_cache = collections.OrderedDict()

        self._thread = None

    def set_image(self, image: np.ndarray):
        with self._lock:
            self._image = image
            self._image_embedding = self._image_embedding_cache.get(
                self._image.tobytes()
            )

        if self._image_embedding is None:
            self._thread = threading.Thread(
                target=self._compute_and_cache_image_embedding
            )
            self._thread.start()

    def _compute_and_cache_image_embedding(self):
        with self._lock:
            logger.debug("Computing image embedding...")
            self._image_embedding = _compute_image_embedding(
                image_size=self._image_size,
                encoder_session=self._encoder_session,
                image=self._image,
            )
            if len(self._image_embedding_cache) > 10:
                self._image_embedding_cache.popitem(last=False)
            self._image_embedding_cache[
                self._image.tobytes()
            ] = self._image_embedding
            logger.debug("Done computing image embedding.")

    def _get_image_embedding(self):
        if self._thread is not None:
            self._thread.join()
            self._thread = None
        with self._lock:
            return self._image_embedding

    def predict_polygon_from_points(self, points, point_labels):
        image_embedding = self._get_image_embedding()
        polygon = _compute_polygon_from_points(
            image_size=self._image_size,
            decoder_session=self._decoder_session,
            image=self._image,
            image_embedding=image_embedding,
            points=points,
            point_labels=point_labels,
        )
        return polygon


def _compute_scale_to_resize_image(image_size, image):
    height, width = image.shape[:2]
    if width > height:
        scale = image_size / width
        new_height = int(round(height * scale))
        new_width = image_size
    else:
        scale = image_size / height
        new_height = image_size
        new_width = int(round(width * scale))
    return scale, new_height, new_width


def _resize_image(image_size, image):
    scale, new_height, new_width = _compute_scale_to_resize_image(
        image_size=image_size, image=image
    )
    scaled_image = imgviz.resize(
        image,
        height=new_height,
        width=new_width,
        backend="pillow",
    ).astype(np.float32)
    return scale, scaled_image


def _compute_image_embedding(image_size, encoder_session, image):
    image = imgviz.asrgb(image)

    scale, x = _resize_image(image_size, image)
    x = (x - np.array([123.675, 116.28, 103.53], dtype=np.float32)) / np.array(
        [58.395, 57.12, 57.375], dtype=np.float32
    )
    x = np.pad(
        x,
        (
            (0, image_size - x.shape[0]),
            (0, image_size - x.shape[1]),
            (0, 0),
        ),
    )
    x = x.transpose(2, 0, 1)[None, :, :, :]

    output = encoder_session.run(output_names=None, input_feed={"x": x})
    image_embedding = output[0]

    return image_embedding


def _get_contour_length(contour):
    contour_start = contour
    contour_end = np.r_[contour[1:], contour[0:1]]
    return np.linalg.norm(contour_end - contour_start, axis=1).sum()


def _compute_polygon_from_points(
    image_size, decoder_session, image, image_embedding, points, point_labels
):
    input_point = np.array(points, dtype=np.float32)
    input_label = np.array(point_labels, dtype=np.int32)

    onnx_coord = np.concatenate([input_point, np.array([[0.0, 0.0]])], axis=0)[
        None, :, :
    ]
    onnx_label = np.concatenate([input_label, np.array([-1])], axis=0)[
        None, :
    ].astype(np.float32)

    scale, new_height, new_width = _compute_scale_to_resize_image(
        image_size=image_size, image=image
    )
    onnx_coord = (
        onnx_coord.astype(float)
        * (new_width / image.shape[1], new_height / image.shape[0])
    ).astype(np.float32)

    onnx_mask_input = np.zeros((1, 1, 256, 256), dtype=np.float32)
    onnx_has_mask_input = np.array([-1], dtype=np.float32)

    decoder_inputs = {
        "image_embeddings": image_embedding,
        "point_coords": onnx_coord,
        "point_labels": onnx_label,
        "mask_input": onnx_mask_input,
        "has_mask_input": onnx_has_mask_input,
        "orig_im_size": np.array(image.shape[:2], dtype=np.float32),
    }

    masks, _, _ = decoder_session.run(None, decoder_inputs)
    mask = masks[0, 0]  # (1, 1, H, W) -> (H, W)
    mask = mask > 0.0
    if 0:
        imgviz.io.imsave(
            "mask.jpg", imgviz.label2rgb(mask, imgviz.rgb2gray(image))
        )

    contours = skimage.measure.find_contours(np.pad(mask, pad_width=1))
    contour = max(contours, key=_get_contour_length)
    polygon = skimage.measure.approximate_polygon(
        coords=contour,
        tolerance=np.ptp(contour, axis=0).max() / 100,
    )
    polygon = np.clip(polygon, (0, 0), (mask.shape[0] - 1, mask.shape[1] - 1))
    polygon = polygon[:-1]  # drop last point that is duplicate of first point
    if 0:
        image_pil = PIL.Image.fromarray(image)
        imgviz.draw.line_(image_pil, yx=polygon, fill=(0, 255, 0))
        for point in polygon:
            imgviz.draw.circle_(
                image_pil, center=point, diameter=10, fill=(0, 255, 0)
            )
        imgviz.io.imsave("contour.jpg", np.asarray(image_pil))

    return polygon[:, ::-1]  # yx -> xy
