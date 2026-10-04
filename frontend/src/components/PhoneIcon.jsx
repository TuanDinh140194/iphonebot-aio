function PhoneIcon() {
  return (
    <svg width="24" height="38" viewBox="0 0 62 98" fill="none">
      <defs>
        <linearGradient id="phoneBody" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="#1e2740" />
          <stop offset="100%" stopColor="#0a0e1a" />
        </linearGradient>
      </defs>

      <rect
        x="1"
        y="1"
        width="60"
        height="96"
        rx="16"
        fill="url(#phoneBody)"
        stroke="#38bdf8"
        strokeWidth="1"
        strokeOpacity="0.5"
      />
      <rect
        x="8"
        y="9"
        width="46"
        height="80"
        rx="8"
        fill="none"
        stroke="#38bdf8"
        strokeWidth="1"
        strokeOpacity="0.3"
      />
      <circle
        cx="31"
        cy="20"
        r="4"
        fill="none"
        stroke="#38bdf8"
        strokeWidth="1.2"
      />
      <circle cx="31" cy="20" r="1.4" fill="#38bdf8" />
    </svg>
  );
}

export default PhoneIcon;
