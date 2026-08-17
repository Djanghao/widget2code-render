export default function Widget() {
  return (
    <div style={{width: 240, height: 240, background: '#101418', borderRadius: 16,
                 display: 'flex', alignItems: 'center', justifyContent: 'center',
                 overflow: 'hidden'}}>
      <svg width="200" height="200" viewBox="0 0 200 200">
        <circle cx="100" cy="100" r="80" fill="none" stroke="#2d3a4a" strokeWidth="14" />
        <circle cx="100" cy="100" r="80" fill="none" stroke="#4caf50" strokeWidth="14"
                strokeDasharray="352 503" strokeLinecap="round"
                transform="rotate(-90 100 100)" />
        <path d="M60 130 L90 100 L115 118 L150 70" fill="none" stroke="#ffb300"
              strokeWidth="4" strokeLinejoin="round" strokeLinecap="round" />
        <polygon points="100,20 108,38 90,38" fill="#ee6666" />
        <text x="100" y="106" textAnchor="middle" fill="#fff" fontSize="28"
              fontFamily="Arial, sans-serif" fontWeight="700">70%</text>
      </svg>
    </div>
  );
}
