import { PiEyeBold } from 'react-icons/pi';
import { SiReact } from 'react-icons/si';
import { AreaChart, Area, YAxis, ResponsiveContainer } from 'recharts';

export default function Widget() {
  const data = [{ v: 12 }, { v: 30 }, { v: 18 }, { v: 41 }, { v: 27 }];

  return (
    <div style={{
      width: '360px',
      height: '180px',
      position: 'relative',
      overflow: 'hidden',
      boxSizing: 'border-box',
      background: '#ffffff',
      padding: '16px',
      fontFamily: 'DejaVu Sans, sans-serif',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '12px' }}>
        <PiEyeBold size={20} color="#334155" />
        <SiReact size={20} color="#334155" />
        <span style={{ fontSize: '14px', color: '#334155' }}>imports</span>
      </div>
      <div style={{ width: '328px', height: '104px' }}>
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={data}>
            <YAxis hide domain={[0, 50]} />
            <Area dataKey="v" stroke="#2563eb" fill="#bfdbfe" isAnimationActive={false} />
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
