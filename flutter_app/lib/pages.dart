import 'package:flutter/material.dart';

import 'brain_client.dart';
import 'prefs.dart';

const Color _accent = Color(0xFF4FC3F7);
const Color _hint = Color(0xFF8FA6C4);

/// 通用页面骨架：标题 + 说明 + 内容。
class PageFrame extends StatelessWidget {
  const PageFrame({super.key, required this.title, required this.subtitle, required this.child});
  final String title;
  final String subtitle;
  final Widget child;

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.all(22),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            Text(title, style: const TextStyle(fontSize: 22, fontWeight: FontWeight.w600, color: Colors.white)),
            const SizedBox(height: 6),
            Text(subtitle, style: const TextStyle(fontSize: 12, color: _hint)),
            const SizedBox(height: 18),
            Expanded(child: child),
          ],
        ),
      );
}

class _Card extends StatelessWidget {
  const _Card({required this.child});
  final Widget child;

  @override
  Widget build(BuildContext context) => Container(
        width: double.infinity,
        padding: const EdgeInsets.all(16),
        decoration: BoxDecoration(
          color: const Color(0xFF0A1424),
          borderRadius: BorderRadius.circular(12),
          border: Border.all(color: const Color(0xFF1B2A42)),
        ),
        child: child,
      );
}

/// 记忆管理：列表 + 写入（走 /v1/events 受理，最终结果由 WS 点亮星云）。
class MemoriesPage extends StatefulWidget {
  const MemoriesPage({super.key, required this.client});
  final BrainClient client;

  @override
  State<MemoriesPage> createState() => _MemoriesPageState();
}

class _MemoriesPageState extends State<MemoriesPage> {
  final TextEditingController _input = TextEditingController();
  List<MemoryRow> _rows = <MemoryRow>[];
  String _status = '—';

  @override
  void initState() {
    super.initState();
    _reload();
    widget.client.events.listen((event) {
      if (event['type'] == 'node_created' && mounted) _reload();
    });
  }

  Future<void> _reload() async {
    try {
      final rows = await widget.client.memories();
      if (mounted) setState(() => _rows = rows);
    } catch (error) {
      if (mounted) setState(() => _status = '读取失败：$error');
    }
  }

  Future<void> _save() async {
    final content = _input.text.trim();
    if (content.isEmpty) return;
    try {
      final result = await widget.client.saveMemory(content);
      _input.clear();
      setState(() => _status = '已受理 ${result['event_id']}（${result['ack_ms']} ms）→ 后台处理中');
    } catch (error) {
      setState(() => _status = '写入失败：$error');
    }
  }

  @override
  Widget build(BuildContext context) => PageFrame(
        title: '记忆管理',
        subtitle: '写入走完整链路（丘脑路由 → 语义合并 → 六引擎投影）；受理后由后台 Worker 完成，星云会点亮新节点',
        child: Column(
          children: <Widget>[
            _Card(
              child: Row(
                children: <Widget>[
                  Expanded(
                    child: TextField(
                      controller: _input,
                      style: const TextStyle(color: Colors.white, fontSize: 13),
                      decoration: const InputDecoration(
                        hintText: '输入一条新记忆…',
                        hintStyle: TextStyle(color: Color(0xFF63788F), fontSize: 13),
                        border: InputBorder.none,
                      ),
                      onSubmitted: (_) => _save(),
                    ),
                  ),
                  const SizedBox(width: 12),
                  FilledButton(onPressed: _save, child: const Text('写入记忆')),
                  const SizedBox(width: 12),
                  OutlinedButton(onPressed: _reload, child: const Text('刷新')),
                ],
              ),
            ),
            const SizedBox(height: 10),
            Align(alignment: Alignment.centerLeft, child: Text(_status, style: const TextStyle(color: _hint, fontSize: 11))),
            const SizedBox(height: 10),
            Expanded(
              child: _Card(
                child: ListView.separated(
                  itemCount: _rows.length,
                  separatorBuilder: (_, __) => const Divider(color: Color(0xFF16243A), height: 1),
                  itemBuilder: (context, index) {
                    final row = _rows[index];
                    return ListTile(
                      leading: Icon(
                        row.memoryType == 'emotion' ? Icons.favorite : Icons.memory,
                        color: row.memoryType == 'emotion' ? const Color(0xFFFF4D7D) : _accent,
                        size: 18,
                      ),
                      title: Text(row.content, style: const TextStyle(color: Colors.white, fontSize: 13)),
                      subtitle: Text('${row.memoryType} · ${row.tier} · PAD ${row.pad.map((v) => v.toStringAsFixed(1)).join('/')}',
                          style: const TextStyle(color: _hint, fontSize: 10)),
                    );
                  },
                ),
              ),
            ),
          ],
        ),
      );
}

/// 召回测试：结果分数 + 命中通道，可在星云页搜索触发光脉冲。
class RecallPage extends StatefulWidget {
  const RecallPage({super.key, required this.client});
  final BrainClient client;

  @override
  State<RecallPage> createState() => _RecallPageState();
}

class _RecallPageState extends State<RecallPage> {
  final TextEditingController _query = TextEditingController();
  List<RecallHit> _hits = <RecallHit>[];
  String _status = '输入查询后回车';

  Future<void> _run() async {
    try {
      final hits = await widget.client.recall(_query.text);
      setState(() {
        _hits = hits;
        _status = '命中 ${hits.length} 条（字符 2-gram 余弦，只读投影、不加载模型）';
      });
    } catch (error) {
      setState(() => _status = '召回失败：$error');
    }
  }

  @override
  Widget build(BuildContext context) => PageFrame(
        title: '召回测试',
        subtitle: '在线受理路径不含检索计算；这里直接查询只读投影，星云页搜索会用同样结果发射光脉冲',
        child: Column(
          children: <Widget>[
            _Card(
              child: Row(
                children: <Widget>[
                  Expanded(
                    child: TextField(
                      controller: _query,
                      style: const TextStyle(color: Colors.white, fontSize: 13),
                      decoration: const InputDecoration(hintText: '例如：发版冲突', hintStyle: TextStyle(color: Color(0xFF63788F)), border: InputBorder.none),
                      onSubmitted: (_) => _run(),
                    ),
                  ),
                  FilledButton(onPressed: _run, child: const Text('召回')),
                ],
              ),
            ),
            const SizedBox(height: 10),
            Align(alignment: Alignment.centerLeft, child: Text(_status, style: const TextStyle(color: _hint, fontSize: 11))),
            const SizedBox(height: 10),
            Expanded(
              child: _Card(
                child: ListView.builder(
                  itemCount: _hits.length,
                  itemBuilder: (context, index) {
                    final hit = _hits[index];
                    return ListTile(
                      title: Text(hit.content, style: const TextStyle(color: Colors.white, fontSize: 13)),
                      trailing: Text(hit.score.toStringAsFixed(3), style: const TextStyle(color: _accent, fontSize: 12)),
                      subtitle: Text('${hit.memoryType} · fts', style: const TextStyle(color: _hint, fontSize: 10)),
                    );
                  },
                ),
              ),
            ),
          ],
        ),
      );
}

/// 接口与端口：显示后端地址、健康状态、curl 示例。
class InterfacePage extends StatefulWidget {
  const InterfacePage({super.key, required this.client});
  final BrainClient client;

  @override
  State<InterfacePage> createState() => _InterfacePageState();
}

class _InterfacePageState extends State<InterfacePage> {
  Map<String, dynamic> _health = <String, dynamic>{};
  String _error = '';

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    try {
      final health = await widget.client.health();
      if (mounted) setState(() => _health = health);
    } catch (error) {
      if (mounted) setState(() => _error = '$error');
    }
  }

  @override
  Widget build(BuildContext context) {
    final base = widget.client.base;
    return PageFrame(
      title: '接口与端口',
      subtitle: 'Flutter 只是外壳；所有数据来自 v3 Daemon（HTTP + WebSocket）',
      child: ListView(
        children: <Widget>[
          _Card(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: <Widget>[
                Text('HTTP  $base', style: const TextStyle(color: _accent, fontSize: 13)),
                Text('WS    ${widget.client.wsUrl}', style: const TextStyle(color: _accent, fontSize: 13)),
                const SizedBox(height: 10),
                Text(_error.isEmpty ? '内核状态：${_health['ok'] == true ? '正常' : '未知'}' : '连接失败：$_error',
                    style: const TextStyle(color: _hint, fontSize: 12)),
                if (_health.isNotEmpty)
                  Text('inbox ${_health['inbox']} · worker ${_health['worker']} · journal ${_health['journal_events']}',
                      style: const TextStyle(color: _hint, fontSize: 11)),
              ],
            ),
          ),
          const SizedBox(height: 12),
          _Card(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: <Widget>[
                const Text('写入一条记忆（curl）', style: TextStyle(color: Colors.white, fontSize: 13)),
                const SizedBox(height: 8),
                SelectableText('curl -X POST $base/v1/events -H "Content-Type: application/json" -d "{\"content\":\"测试记忆\",\"agent_id\":\"flutter\"}"',
                    style: const TextStyle(color: Color(0xFF7FE9FF), fontSize: 11)),
                const SizedBox(height: 14),
                const Text('MCP / HTTP 工具：save_memory · recall_memory · timeline_query · profile_read · conflict_report · health_check',
                    style: TextStyle(color: _hint, fontSize: 11)),
              ],
            ),
          ),
          const SizedBox(height: 12),
          OutlinedButton(onPressed: _load, child: const Text('重新检测')),
        ],
      ),
    );
  }
}

/// 设置：后端地址/端口 + 3D 星云参数（同步到后端 nebula_prefs.json，跨设备一致）。
class SettingsPage extends StatefulWidget {
  const SettingsPage({super.key, required this.client, required this.onReconnect});
  final BrainClient client;
  final void Function(String host, int httpPort, int wsPort) onReconnect;

  @override
  State<SettingsPage> createState() => _SettingsPageState();
}

class _SettingsPageState extends State<SettingsPage> {
  late final TextEditingController _host = TextEditingController(text: Prefs.get<String>('host', '127.0.0.1'));
  late final TextEditingController _http = TextEditingController(text: Prefs.get<int>('http_port', 8768).toString());
  late final TextEditingController _ws = TextEditingController(text: Prefs.get<int>('ws_port', 8767).toString());
  double _focal = 900;
  double _starSize = 3.2;
  double _spin = 0.00035;
  bool _bloom = true;
  bool _autoRotate = true;
  String _status = '—';

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    try {
      final config = await widget.client.config();
      final nebula = config['nebula'] as Map<String, dynamic>? ?? <String, dynamic>{};
      if (!mounted) return;
      setState(() {
        _focal = (nebula['focal_length'] as num? ?? 900).toDouble();
        _starSize = (nebula['star_size'] as num? ?? 3.2).toDouble();
        _spin = (nebula['spin_speed'] as num? ?? 0.00035).toDouble();
        _bloom = nebula['bloom'] != false;
        _autoRotate = nebula['auto_rotate'] != false;
      });
    } catch (_) {
      // 后端未启动时保留本地默认值
    }
  }

  void _save() {
    final host = _host.text.trim().isEmpty ? '127.0.0.1' : _host.text.trim();
    final httpPort = int.tryParse(_http.text) ?? 8768;
    final wsPort = int.tryParse(_ws.text) ?? 8767;
    Prefs.save(<String, dynamic>{'host': host, 'http_port': httpPort, 'ws_port': wsPort});
    widget.onReconnect(host, httpPort, wsPort);
    setState(() => _status = '连接参数已保存并重新连接');
  }

  Future<void> _saveNebula() async {
    final patch = <String, dynamic>{
      'nebula': <String, dynamic>{
        'focal_length': _focal,
        'star_size': _starSize,
        'spin_speed': _spin,
        'bloom': _bloom,
        'auto_rotate': _autoRotate,
      }
    };
    Prefs.save(<String, dynamic>{'nebula': patch['nebula']});
    try {
      await widget.client.saveConfig(patch);
      setState(() => _status = '3D 参数已同步到后端（刷新星云页生效）');
    } catch (error) {
      setState(() => _status = '已存本地；后端未连接：$error');
    }
  }

  @override
  Widget build(BuildContext context) => PageFrame(
        title: '设置',
        subtitle: '连接参数写在 %APPDATA%/super-brain/flutter_prefs.json；3D 参数同时同步到后端，多端一致',
        child: ListView(
          children: <Widget>[
            _Card(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: <Widget>[
                  const Text('后端连接', style: TextStyle(color: Colors.white, fontSize: 14)),
                  const SizedBox(height: 12),
                  Row(
                    children: <Widget>[
                      Expanded(flex: 3, child: _field('主机', _host)),
                      const SizedBox(width: 12),
                      Expanded(child: _field('HTTP 端口', _http)),
                      const SizedBox(width: 12),
                      Expanded(child: _field('WS 端口', _ws)),
                    ],
                  ),
                  const SizedBox(height: 12),
                  FilledButton(onPressed: _save, child: const Text('保存并重新连接')),
                ],
              ),
            ),
            const SizedBox(height: 12),
            _Card(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: <Widget>[
                  const Text('3D 星云', style: TextStyle(color: Colors.white, fontSize: 14)),
                  _slider('焦距（透视强度）', _focal, 400, 1600, (v) => setState(() => _focal = v)),
                  _slider('星点大小', _starSize, 1, 8, (v) => setState(() => _starSize = v)),
                  _slider('自转速度', _spin * 10000, 1, 20, (v) => setState(() => _spin = v / 10000)),
                  SwitchListTile(title: const Text('加色辉光 Bloom', style: TextStyle(color: _hint, fontSize: 12)), value: _bloom, onChanged: (v) => setState(() => _bloom = v)),
                  SwitchListTile(title: const Text('自动缓慢旋转', style: TextStyle(color: _hint, fontSize: 12)), value: _autoRotate, onChanged: (v) => setState(() => _autoRotate = v)),
                  const SizedBox(height: 8),
                  FilledButton(onPressed: _saveNebula, child: const Text('保存 3D 参数')),
                ],
              ),
            ),
            const SizedBox(height: 10),
            Text(_status, style: const TextStyle(color: _hint, fontSize: 11)),
          ],
        ),
      );

  Widget _field(String label, TextEditingController controller) => TextField(
        controller: controller,
        style: const TextStyle(color: Colors.white, fontSize: 12),
        decoration: InputDecoration(labelText: label, labelStyle: const TextStyle(color: _hint, fontSize: 11), border: const OutlineInputBorder()),
      );

  Widget _slider(String label, double value, double min, double max, ValueChanged<double> onChanged) => Row(
        children: <Widget>[
          SizedBox(width: 130, child: Text(label, style: const TextStyle(color: _hint, fontSize: 11))),
          Expanded(child: Slider(value: value.clamp(min, max).toDouble(), min: min, max: max, onChanged: onChanged)),
          SizedBox(width: 56, child: Text(value.toStringAsFixed(value > 10 ? 0 : 4), style: const TextStyle(color: _accent, fontSize: 11))),
        ],
      );
}

class AboutPage extends StatelessWidget {
  const AboutPage({super.key});
  @override
  Widget build(BuildContext context) => const PageFrame(
        title: '关于',
        subtitle: 'v3 事件驱动记忆内核 · Flutter 外壳',
        child: _Card(
          child: Text(
            '交互层：Flutter 3D 星云（纯 Dart CustomPainter + 旋转矩阵 + 透视投影，零第三方依赖）\n'
            '控制面：Windows 服务（WinSW）托管 Daemon，Daemon 内管 Model Worker\n'
            '数据面：NDJSON journal（唯一真源）+ SQLite WAL inbox（租约队列）+ 可选 Honker 闹钟\n'
            '计算面：Worker 消费事件 → 抽取/语义合并/PAD/FSRS/六库投影 → WS 广播增量\n\n'
            '写路径：journal fsync → inbox → 202 accepted（不等待模型）；最终结果由 WS 推送。\n'
            'Flutter 只经 HTTP/WebSocket 访问内核，不直连任何存储或模型。',
            style: TextStyle(color: _hint, fontSize: 13, height: 1.8),
          ),
        ),
      );
}
