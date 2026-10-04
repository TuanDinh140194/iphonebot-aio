function Logo() {
  return (
    <svg width="48" height="48" viewBox="0 0 48 48" fill="none">
      <defs>
        <linearGradient id="logoGradDark" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="#1e2740" />
          <stop offset="100%" stopColor="#0a0e1a" />
        </linearGradient>
        {/*         <filter id="neonGlow" x="-20%" y="-20%" width="220%" height="220%"> */}
        {/*           <feGaussianBlur stdDeviation="1.5" result="blur1" /> */}
        {/*           <feGaussianBlur stdDeviation="4" result="blur2" /> */}
        {/*           <feMerge> */}
        {/*             <feMergeNode in="blur2" /> */}
        {/*             <feMergeNode in="blur1" /> */}
        {/*             <feMergeNode in="SourceGraphic" /> */}
        {/*           </feMerge> */}
        {/*         </filter> */}
      </defs>

      <rect
        x="1"
        y="1"
        width="46"
        height="46"
        rx="14"
        fill="url(#logoGradDark)"
        stroke="#38bdf8"
        strokeWidth="1"
        strokeOpacity="0.4"
      />
      <text
        x="24"
        y="31"
        textAnchor="middle"
        fontSize="19"
        fontWeight="700"
        fontFamily="system-ui, sans-serif"
        fill="#38bdf8"
      >
        18
      </text>
    </svg>
  );
}

export default Logo;
