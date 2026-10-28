const endpoint = '/__serein/appearance/images';
const maximum = 2 * 1024 * 1024;

async function result(response) {
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : '图片未保存，请检查连接后重试。');
  return body;
}

export async function loadImages() {
  return (await result(await fetch(endpoint+'?metadata=1', {
    cache: 'no-store', signal: AbortSignal.timeout(20_000),
  }))).images;
}

function dataUrl(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error('图片读取失败，请重新选择。'));
    reader.readAsDataURL(blob);
  });
}

export async function prepareImage(file, key) {
  if (!['image/png', 'image/jpeg', 'image/webp', 'image/gif'].includes(file.type)) throw new Error('请选择 PNG、JPEG、WebP 或 GIF 图片。');
  if (file.size > 20 * 1024 * 1024) throw new Error('原图不能超过 20 MiB，请先缩小图片。');
  const url = URL.createObjectURL(file);
  try {
    const image = new Image(); image.src = url;
    try { await image.decode(); } catch { throw new Error('无法读取这张图片，请换一张重试。'); }
    if (file.size <= maximum) return await dataUrl(file);
    if (file.type === 'image/gif') throw new Error('GIF 动图不能超过 2 MiB，请先缩小图片。');
    const scale = Math.min(1, (key === 'hero' ? 2560 : 1024) / Math.max(image.naturalWidth, image.naturalHeight));
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
    canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
    canvas.getContext('2d').drawImage(image, 0, 0, canvas.width, canvas.height);
    let blob;
    for (const quality of [.85, .65, .45]) {
      blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/webp', quality));
      if (blob && blob.size <= maximum) break;
    }
    if (!blob || blob.size > maximum) throw new Error('图片仍然过大，请缩小后重试。');
    return await dataUrl(blob);
  } finally { URL.revokeObjectURL(url); }
}

export async function saveImage(key, file) {
  const data_url = await prepareImage(file, key);
  return (await result(await fetch(`${endpoint}/${key}`, {method: 'PUT',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({data_url})}))).data_url;
}
