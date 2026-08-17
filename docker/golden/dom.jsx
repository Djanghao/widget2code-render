export default function Widget() {
  return (
    <div style={{width: 360, height: 200, background: 'linear-gradient(135deg, #1a2b3c, #2d4a6b)',
                 borderRadius: 12, padding: 20, color: '#fff', overflow: 'hidden',
                 fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
                 boxShadow: '0 4px 12px rgba(0,0,0,0.3)', display: 'flex',
                 flexDirection: 'column', gap: 10}}>
      <div style={{display: 'flex', justifyContent: 'space-between', alignItems: 'center'}}>
        <span style={{fontSize: 18, fontWeight: 600}}>Revenue</span>
        <span style={{fontSize: 12, background: '#4caf50', borderRadius: 10,
                      padding: '2px 10px'}}>+12.4%</span>
      </div>
      <div style={{fontSize: 34, fontWeight: 700, letterSpacing: '-0.5px'}}>$48,291.07</div>
      <div style={{display: 'flex', gap: 6, marginTop: 'auto'}}>
        {[62, 45, 78, 30, 55, 90, 40].map((h, i) => (
          <div key={i} style={{flex: 1, height: 48, display: 'flex', alignItems: 'flex-end'}}>
            <div style={{width: '100%', height: `${h}%`, background: 'rgba(255,255,255,0.65)',
                         borderRadius: 3}} />
          </div>
        ))}
      </div>
    </div>
  );
}
