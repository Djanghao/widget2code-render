export default function Widget() {
  const stacks = [
    '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
    'Georgia, "Times New Roman", serif',
    '"SF Mono", "Fira Code", Menlo, monospace',
    'Verdana, Geneva, sans-serif',
  ];
  return (
    <div style={{width: 420, height: 300, background: '#fff', padding: 16,
                 border: '1px solid #ddd', overflow: 'hidden', display: 'flex',
                 flexDirection: 'column', gap: 8}}>
      {stacks.map((ff, i) => (
        <div key={i} style={{fontFamily: ff, borderBottom: '1px solid #eee', paddingBottom: 6}}>
          <div style={{fontSize: 16, fontWeight: 700, color: '#1a1a1a'}}>
            The quick brown fox 0123456789
          </div>
          <div style={{fontSize: 12, fontStyle: 'italic', color: '#666'}}>
            jumps over the lazy dog — Ilion §¶µ ﬁﬂ 汉字テスト
          </div>
          <div style={{fontSize: 10, letterSpacing: '0.5px', color: '#999'}}>
            WAVE Tally offset kerning AV To 1Il| O0 rn m
          </div>
        </div>
      ))}
    </div>
  );
}
