import { useId } from "react";
import { darkroomGlyphs } from "./darkroomGlyphs.js";

const letters = [
  ["未", 38, 30, 106],
  ["寄", 145, 123, 106],
  ["出", 38, 232, 106],
  ["的", 164, 247, 64],
  ["信", 224, 316, 106],
];

export function DarkroomTitle() {
  const id = useId().replaceAll(":", "");
  return <svg className="darkroom-title-art" viewBox="0 0 360 438" aria-hidden="true" focusable="false">
    <defs>
      <filter id={`${id}-ink`} x="-70%" y="-90%" width="240%" height="280%">
        <feGaussianBlur stdDeviation="14 7" />
      </filter>
      <filter id={`${id}-glow`} x="-50%" y="-50%" width="200%" height="200%">
        <feGaussianBlur stdDeviation="2.4" />
      </filter>
      <pattern id={`${id}-checks`} width="6" height="6" patternUnits="userSpaceOnUse">
        <path d="M0 0h3v3H0zM3 3h3v3H3z" fill="#000" />
      </pattern>
      <mask id={`${id}-paper`}>
        <rect width="360" height="438" fill="#fff" />
        <rect x="307" y="398" width="18" height="12" fill={`url(#${id}-checks)`} />
      </mask>
    </defs>
    <g className="darkroom-title-art__grid" fill="none" stroke="currentColor" strokeWidth=".6">
      <path d="M29 15V348M141 15V109M141 221V352M254 108V298M337 300V425M18 24H159M29 137H128M251 113H272M29 225H157M141 237H245M18 349H212M222 309H348M141 420H348" />
      <path d="M22 24H48M29 17V42M134 225H160M141 218V241M330 420H347M337 403V427" strokeOpacity=".9" />
    </g>
    <g fill="currentColor" mask={`url(#${id}-paper)`}>
      {letters.map(([letter, x, y, size]) => {
        const glyph = darkroomGlyphs[letter];
        return <g key={letter} transform={`translate(${x} ${y + size * .85}) scale(${size / 1000} ${-size / 1000})`}>
          <path d={glyph.body} />
          {glyph.stroke && <path className="darkroom-title-art__stroke" d={glyph.stroke} filter={`url(#${id}-ink)`} />}
        </g>;
      })}
    </g>
    <path d="M138 224h10" stroke="currentColor" strokeWidth="1.2" filter={`url(#${id}-glow)`} opacity=".5" />
  </svg>;
}
