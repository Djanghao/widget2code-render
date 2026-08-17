export default function Widget() {
  const option = {
    animation: false,
    grid: {left: 40, right: 16, top: 24, bottom: 28},
    xAxis: {type: 'category', data: ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']},
    yAxis: {type: 'value'},
    series: [
      {type: 'bar', data: [120, 200, 150, 80, 70, 110, 130], itemStyle: {color: '#5470c6'}},
      {type: 'line', data: [90, 160, 170, 120, 100, 140, 150], smooth: true,
       lineStyle: {color: '#ee6666', width: 2}, symbolSize: 6},
    ],
  };
  return (
    <div style={{width: 420, height: 260, background: '#fff', borderRadius: 8,
                 border: '1px solid #e0e0e0', overflow: 'hidden'}}>
      <ReactECharts option={option} style={{width: '100%', height: '100%'}} />
    </div>
  );
}
