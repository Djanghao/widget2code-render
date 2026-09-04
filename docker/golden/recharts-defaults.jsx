// The canary for what the other four cannot see: that a chart written with
// recharts' *defaults* is drawn the same way twice, and drawn finished.
//
// Two properties, one widget:
//
//   `isAnimationActive` is deliberately absent. Recharts 3 defaults it to
//   'auto' and resolves that against `prefers-reduced-motion`, so unless the
//   renderer emulates reduced motion this chart animates, and a screenshot of
//   an animating chart is a different picture every time -- 24 renders of this
//   widget returned three.
//
//   The bars are rounded, sit on a rounded background track, and are laid out
//   by ResponsiveContainer inside an odd box, so every edge is antialiased at a
//   fractional coordinate. That is where a tile carried over from an earlier
//   frame shows: with partial raster left on, this widget renders to a
//   different hash -- stably, so the reference checksum alone catches it.
//
// Both regressions therefore fail this canary rather than a collection.
export default function Widget() {
  const data = [{val: 40}, {val: 87}, {val: 60}, {val: 48}, {val: 67}];
  const colors = ['#FD4B50', '#8B56FE', '#FD4B50', '#8B56FE', '#8B56FE'];
  return (
    <div style={{width: 507, height: 380, background: '#312D4B', borderRadius: 56,
                 position: 'relative', overflow: 'hidden', boxSizing: 'border-box'}}>
      <div style={{position: 'absolute', top: 41, left: 53, width: 340, height: 240}}>
        <Recharts.ResponsiveContainer width="100%" height="100%">
          <Recharts.BarChart data={data} margin={{top: 0, right: 0, left: 0, bottom: 0}}>
            <Recharts.YAxis domain={[0, 100]} hide />
            <Recharts.Bar dataKey="val" barSize={20} radius={[10, 10, 10, 10]}
                          background={{fill: '#484461', radius: [10, 10, 10, 10]}}>
              {colors.map((fill, i) => <Recharts.Cell key={i} fill={fill} />)}
            </Recharts.Bar>
          </Recharts.BarChart>
        </Recharts.ResponsiveContainer>
      </div>
    </div>
  );
}
