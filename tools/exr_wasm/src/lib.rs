use exr::image::pixel_vec::PixelVec;
use exr::prelude::*;
use std::io::Cursor;
use std::sync::{Mutex, OnceLock};

struct DecodedImage {
    width: u32,
    height: u32,
    pixels: Vec<f32>,
}

#[derive(Default)]
struct DecodeState {
    image: Option<DecodedImage>,
    error: Vec<u8>,
}

fn state() -> &'static Mutex<DecodeState> {
    static STATE: OnceLock<Mutex<DecodeState>> = OnceLock::new();
    STATE.get_or_init(|| Mutex::new(DecodeState::default()))
}

#[no_mangle]
pub extern "C" fn typhoon_exr_alloc(size: usize) -> *mut u8 {
    if size == 0 {
        return std::ptr::null_mut();
    }
    let mut buffer = Vec::<u8>::with_capacity(size);
    let ptr = buffer.as_mut_ptr();
    std::mem::forget(buffer);
    ptr
}

#[no_mangle]
pub unsafe extern "C" fn typhoon_exr_dealloc(ptr: *mut u8, size: usize) {
    if !ptr.is_null() && size != 0 {
        drop(Vec::from_raw_parts(ptr, 0, size));
    }
}

#[no_mangle]
pub unsafe extern "C" fn typhoon_exr_decode(ptr: *const u8, len: usize) -> i32 {
    if ptr.is_null() || len == 0 {
        set_error("empty EXR input");
        return 0;
    }

    let bytes = std::slice::from_raw_parts(ptr, len);
    match decode_exr(bytes) {
        Ok(image) => {
            let mut state = state().lock().expect("decode state lock poisoned");
            state.image = Some(image);
            state.error.clear();
            1
        }
        Err(error) => {
            set_error(&error);
            0
        }
    }
}

#[no_mangle]
pub extern "C" fn typhoon_exr_width() -> u32 {
    state()
        .lock()
        .expect("decode state lock poisoned")
        .image
        .as_ref()
        .map(|image| image.width)
        .unwrap_or(0)
}

#[no_mangle]
pub extern "C" fn typhoon_exr_height() -> u32 {
    state()
        .lock()
        .expect("decode state lock poisoned")
        .image
        .as_ref()
        .map(|image| image.height)
        .unwrap_or(0)
}

#[no_mangle]
pub extern "C" fn typhoon_exr_pixels_ptr() -> *const f32 {
    state()
        .lock()
        .expect("decode state lock poisoned")
        .image
        .as_ref()
        .map(|image| image.pixels.as_ptr())
        .unwrap_or(std::ptr::null())
}

#[no_mangle]
pub extern "C" fn typhoon_exr_pixels_len() -> usize {
    state()
        .lock()
        .expect("decode state lock poisoned")
        .image
        .as_ref()
        .map(|image| image.pixels.len())
        .unwrap_or(0)
}

#[no_mangle]
pub extern "C" fn typhoon_exr_error_ptr() -> *const u8 {
    state()
        .lock()
        .expect("decode state lock poisoned")
        .error
        .as_ptr()
}

#[no_mangle]
pub extern "C" fn typhoon_exr_error_len() -> usize {
    state()
        .lock()
        .expect("decode state lock poisoned")
        .error
        .len()
}

fn decode_exr(bytes: &[u8]) -> std::result::Result<DecodedImage, String> {
    let image = read()
        .no_deep_data()
        .largest_resolution_level()
        .rgba_channels(
            PixelVec::<(f32, f32, f32, f32)>::constructor,
            PixelVec::set_pixel,
        )
        .first_valid_layer()
        .all_attributes()
        .non_parallel()
        .from_buffered(Cursor::new(bytes))
        .map_err(|error| error.to_string())?;

    let width = image.layer_data.size.width() as u32;
    let height = image.layer_data.size.height() as u32;
    let rgba_pixels = image.layer_data.channel_data.pixels.pixels;
    let mut pixels = Vec::with_capacity(rgba_pixels.len() * 3);
    for (r, g, b, _a) in rgba_pixels {
        pixels.push(r);
        pixels.push(g);
        pixels.push(b);
    }

    Ok(DecodedImage {
        width,
        height,
        pixels,
    })
}

fn set_error(message: &str) {
    let mut state = state().lock().expect("decode state lock poisoned");
    state.image = None;
    state.error.clear();
    state.error.extend_from_slice(message.as_bytes());
}
