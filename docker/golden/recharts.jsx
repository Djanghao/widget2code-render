export default function Widget() {
  const data = [
    {name: 'Q1', uv: 400, pv: 240}, {name: 'Q2', uv: 300, pv: 456},
    {name: 'Q3', uv: 520, pv: 139}, {name: 'Q4', uv: 280, pv: 390},
  ];
  return (
    <div style={{width: 400, height: 240, background: '#fafafa', borderRadius: 8,
                 padding: 8, overflow: 'hidden'}}>
      <Recharts.AreaChart width={384} height={224} data={data}>
        <Recharts.CartesianGrid strokeDasharray="3 3" />
        <Recharts.XAxis dataKey="name" />
        <Recharts.YAxis />
        <Recharts.Area type="monotone" dataKey="uv" stroke="#8884d8" fill="#8884d8"
                       fillOpacity={0.5} isAnimationActive={false} />
        <Recharts.Area type="monotone" dataKey="pv" stroke="#82ca9d" fill="#82ca9d"
                       fillOpacity={0.5} isAnimationActive={false} />
      </Recharts.AreaChart>
    </div>
  );
}
