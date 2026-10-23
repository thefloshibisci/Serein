// The reference varies type size, baseline, brightness and whole-glyph blur.
// Keep this treatment on live text so any letter title can use it.
const rhythm = [
  [1, -1, 1, 0],
  [1.18, 2, .96, .3],
  [.86, -3, .82, 0],
  [.94, 0, .94, 0],
  [1.36, -2, 1, 0],
  [1.12, 3, .94, .4],
  [.86, -2, .78, 0],
  [1, 1, .96, 0],
  [1.12, -1, 1, 0],
  [1.28, 2, 1, 0],
  [.92, -2, .85, .2],
];

export function WhisperTitle({ text }) {
  return <strong className="darkroom-letter-title" aria-label={text}>
    {Array.from(text).map((letter, index) => {
      const [scale, lift, brightness, blur] = rhythm[index % rhythm.length];
      return <span key={index} aria-hidden="true" style={{
        "--glyph-scale": `${scale}em`,
        "--glyph-lift": `${lift}px`,
        "--glyph-brightness": brightness,
        "--glyph-blur": `${blur}px`,
      }}>{letter}</span>;
    })}
  </strong>;
}
