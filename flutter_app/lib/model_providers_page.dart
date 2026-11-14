import 'dart:async';

import 'package:flutter/material.dart';

import 'brain_client.dart';

/// 供应商与内部模型页 —— 模型配置中枢：
/// · 云供应商 CRUD（endpoint/kind/model/key；key 存系统凭据库，明文不落盘）
/// · 内部消费者路由（记忆抽取/精排/巩固 → pool:模型 | free:模型 | provider:名 | 本地）
/// · 本地模型（Ollama endpoint/model）展示
/// 保存即热重载路由层，无需重启后端。
class ModelProvidersPage extends StatefulWidget {
  const ModelProvidersPage({super.key, required this.client});

  final BrainClient client;

  @override
  State<ModelProvidersPage> createState() => _ModelProvidersPageState();
}

class _ModelProvidersPageState extends State<ModelProvidersPage> {
  static const Color _bg = Color(0xFF0B1220);
  static const Color _panel = Color(0xFF121C2E);
  static const Color _border = Color(0xFF22334D);
  static const Color _hint = Color(0xFF7C93B2);
  static const Color _ok = Color(0xFF4ADE80);
  static const Color _dead = Color(0xFFF87171);
  static const Color _accent = Color(0xFF6EE7B7);
  static const Color _warn = Color(0xFFFACC15);

  static const List<String> _tasks = <String>['extract', 'rerank', 'consolidation'];
  static const Map<String, String> _taskLabels = <String, String>{
    'extract': '记忆抽取（报名/登记 LLM）',
    'rerank': '精排（留空=本地 ONNX；配远程卸掉 CPU/显存）',
    'consolidation': '巩固（再巩固窗口 LLM）',
  };

  Map<String, dynamic> _data = <String, dynamic>{};
  Map<String, String> _routes = <String, String>{};
  List<Map<String, dynamic>> _poolModels = <Map<String, dynamic>>[];
  String _message = '';
  bool _busy = false;

  @override
  void initState() {
    super.initState();
    unawaited(_load());
  }

  Future<void> _load() async {
    setState(() => _busy = true);
    try {
      final data = await widget.client.call('router.config', <String, dynamic>{});
      if (!mounted) return;
      setState(() {
        _data = data is Map<String, dynamic> ? data : <String, dynamic>{};
        final raw = _data['task_routes'];
        _routes = raw is Map
            ? {for (final e in raw.entries) e.key.toString(): e.value.toString()}
            : <String, String>{};
        _message = '';
      });
      // 号池目录（下拉选择器数据源）
      try {
        final catalog = await widget.client.call('pool.catalog', <String, dynamic>{});
        if (!mounted) return;
        final disabled = ((catalog['disabled_models'] as List<dynamic>? ?? <dynamic>[]))
            .map((e) => e.toString())
            .toSet();
        final wb = (catalog['wb'] as Map? ?? {});
        setState(() {
          _poolModels = <Map<String, dynamic>>[
            for (final row in (wb['models'] as List<dynamic>? ?? <dynamic>[]))
              if (row is Map && !disabled.contains((row['id'] ?? '').toString()))
                <String, dynamic>{
                  'id': (row['id'] ?? '').toString(),
                  'name': (row['name'] ?? '').toString(),
                },
          ];
        });
      } catch (_) {}
    } catch (error) {
      if (mounted) setState(() => _message = '加载失败：$error');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _providerDialog({Map<String, dynamic>? existing}) async {
    final name = TextEditingController(text: existing?['name']?.toString() ?? '');
    final endpoint =
        TextEditingController(text: existing?['endpoint']?.toString() ?? '');
    final model = TextEditingController(text: existing?['model']?.toString() ?? '');
    final apiKey = TextEditingController();
    var kind = existing?['kind']?.toString() ?? 'cloud';
    final ok = await showDialog<bool>(
      context: context,
      builder: (BuildContext context) => StatefulBuilder(
        builder: (BuildContext context, void Function(void Function()) setDialogState) =>
            AlertDialog(
          backgroundColor: _panel,
          title: Text(existing == null ? '添加供应商' : '编辑供应商 ${existing['name']}',
              style: const TextStyle(color: Colors.white, fontSize: 14)),
          content: SizedBox(
            width: 420,
            child: Column(mainAxisSize: MainAxisSize.min, children: <Widget>[
              _field(name, '名称（唯一，如 deepseek-official）'),
              const SizedBox(height: 8),
              _field(endpoint, 'Endpoint（OpenAI 兼容，如 https://api.xx.com/v1）'),
              const SizedBox(height: 8),
              _field(model, '模型名（如 deepseek-chat）'),
              const SizedBox(height: 8),
              Row(children: <Widget>[
                const Text('类型', style: TextStyle(color: _hint, fontSize: 12)),
                const SizedBox(width: 12),
                ChoiceChip(
                    label: const Text('cloud'),
                    selected: kind == 'cloud',
                    onSelected: (_) => setDialogState(() => kind = 'cloud'),
                    labelStyle: const TextStyle(fontSize: 11)),
                const SizedBox(width: 8),
                ChoiceChip(
                    label: const Text('openai 兼容'),
                    selected: kind == 'openai',
                    onSelected: (_) => setDialogState(() => kind = 'openai'),
                    labelStyle: const TextStyle(fontSize: 11)),
              ]),
              const SizedBox(height: 8),
              _field(apiKey,
                  existing == null || (existing['has_key'] ?? false) == false
                      ? 'API Key（存系统凭据库）'
                      : 'API Key（已保存，留空不改）'),
            ]),
          ),
          actions: <Widget>[
            TextButton(onPressed: () => Navigator.of(context).pop(false),
                child: const Text('取消', style: TextStyle(color: _hint))),
            TextButton(onPressed: () => Navigator.of(context).pop(true),
                child: const Text('保存', style: TextStyle(color: _accent))),
          ],
        ),
      ),
    );
    name.dispose();
    endpoint.dispose();
    model.dispose();
    apiKey.dispose();
    if (ok != true || name.text.trim().isEmpty) return;
    if (!mounted) return;
    try {
      final result = await widget.client.call('router.provider_set', <String, dynamic>{
        'name': name.text.trim(),
        'endpoint': endpoint.text.trim(),
        'kind': kind,
        'model': model.text.trim(),
        'api_key': apiKey.text.trim(),
      });
      if (!mounted) return;
      setState(() => _message = (result is Map && result['ok'] == true)
          ? '供应商已保存并热重载'
          : '保存失败：${result is Map ? (result['error'] ?? '') : ''}');
      unawaited(_load());
    } catch (error) {
      if (mounted) setState(() => _message = '保存失败：$error');
    }
  }

  Widget _field(TextEditingController controller, String hint) => TextField(
        controller: controller,
        style: const TextStyle(color: Colors.white, fontSize: 12),
        decoration: InputDecoration(
          hintText: hint, hintStyle: const TextStyle(color: _hint, fontSize: 11),
          filled: true, fillColor: _bg,
          contentPadding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
          enabledBorder: OutlineInputBorder(borderRadius: BorderRadius.circular(6),
              borderSide: const BorderSide(color: _border)),
          focusedBorder: OutlineInputBorder(borderRadius: BorderRadius.circular(6),
              borderSide: const BorderSide(color: _accent)),
        ),
      );

  @override
  Widget build(BuildContext context) {
    final enabled = (_data['enabled'] ?? false) == true;
    final providers = (_data['providers'] as List<dynamic>? ?? <dynamic>[])
        .whereType<Map>()
        .map(Map<String, dynamic>.from)
        .toList();
    return Scaffold(
      backgroundColor: _bg,
      body: RefreshIndicator(
        onRefresh: _load,
        child: ListView(padding: const EdgeInsets.only(bottom: 16), children: <Widget>[
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 14, 16, 4),
            child: Row(children: <Widget>[
              const Text('供应商与内部模型',
                  style: TextStyle(color: Colors.white, fontSize: 18)),
              const SizedBox(width: 10),
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
                decoration: BoxDecoration(
                  color: (enabled ? _ok : _warn).withValues(alpha: 0.14),
                  borderRadius: BorderRadius.circular(999),
                  border: Border.all(
                      color: (enabled ? _ok : _warn).withValues(alpha: 0.5)),
                ),
                child: Text(enabled ? '云供应商已启用' : '云供应商未启用（.env MODEL_ROUTER_ENABLED=true）',
                    style: TextStyle(color: enabled ? _ok : _warn, fontSize: 10)),
              ),
              const Spacer(),
              IconButton(
                tooltip: '刷新',
                onPressed: _busy ? null : () => unawaited(_load()),
                icon: const Icon(Icons.refresh, color: _hint, size: 20),
              ),
            ]),
          ),
          if (_message.isNotEmpty)
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 0, 16, 4),
              child: Text(_message, style: const TextStyle(color: _accent, fontSize: 11)),
            ),
          // ── 云供应商 ────────────────────────────────────────────────────
          Container(
            margin: const EdgeInsets.fromLTRB(16, 8, 16, 0),
            padding: const EdgeInsets.all(14),
            decoration: BoxDecoration(
              color: _panel, borderRadius: BorderRadius.circular(10),
              border: Border.all(color: _border)),
            child: Column(crossAxisAlignment: CrossAxisAlignment.start,
                children: <Widget>[
              Row(children: <Widget>[
                const Text('云供应商',
                    style: TextStyle(color: Colors.white, fontSize: 14,
                        fontWeight: FontWeight.w600)),
                const Spacer(),
                TextButton.icon(
                  onPressed: () => unawaited(_providerDialog()),
                  icon: const Icon(Icons.add, size: 14, color: _accent),
                  label: const Text('添加供应商',
                      style: TextStyle(color: _accent, fontSize: 12)),
                ),
              ]),
              const SizedBox(height: 6),
              if (providers.isEmpty)
                const Padding(
                  padding: EdgeInsets.symmetric(vertical: 10),
                  child: Text(
                      '还没有云供应商。添加后聊天路由按 intent_map 自动兜底；'
                      '内部消费者（下方路由表）可点名某个供应商。',
                      style: TextStyle(color: _hint, fontSize: 12)),
                )
              else
                ...providers.map((provider) => Padding(
                      padding: const EdgeInsets.symmetric(vertical: 6),
                      child: Row(children: <Widget>[
                        Container(width: 8, height: 8,
                            decoration: BoxDecoration(shape: BoxShape.circle,
                                color: (provider['has_key'] ?? false) == true
                                    ? _ok : _warn)),
                        const SizedBox(width: 8),
                        Expanded(flex: 3,
                            child: Text((provider['name'] ?? '').toString(),
                                style: const TextStyle(
                                    color: Colors.white, fontSize: 12))),
                        Expanded(flex: 4,
                            child: Text(
                                '${provider['kind']} · ${provider['model']}\n'
                                '${provider['endpoint']}',
                                style: const TextStyle(
                                    color: _hint, fontSize: 10, height: 1.3))),
                        IconButton(
                          tooltip: '编辑',
                          onPressed: () =>
                              unawaited(_providerDialog(existing: provider)),
                          icon: const Icon(Icons.edit_outlined,
                              size: 16, color: _hint),
                        ),
                        IconButton(
                          tooltip: '删除',
                          onPressed: () => unawaited(_deleteProvider(
                              (provider['name'] ?? '').toString())),
                          icon: const Icon(Icons.delete_outline,
                              size: 16, color: _dead),
                        ),
                      ]),
                    )),
            ]),
          ),
          // ── 内部消费者路由 ─────────────────────────────────────────────
          Container(
            margin: const EdgeInsets.fromLTRB(16, 10, 16, 0),
            padding: const EdgeInsets.all(14),
            decoration: BoxDecoration(
              color: _panel, borderRadius: BorderRadius.circular(10),
              border: Border.all(color: _border)),
            child: Column(crossAxisAlignment: CrossAxisAlignment.start,
                children: <Widget>[
              const Text('内部消费者 → 供应商路由',
                  style: TextStyle(color: Colors.white, fontSize: 14,
                      fontWeight: FontWeight.w600)),
              const SizedBox(height: 4),
              const Text(
                  '后端小模型不必只吃本地：给每个任务点名一个远程供应商。'
                  'spec 填 pool:模型名（号池 failover）、'
                  'provider:名称（云供应商）；留空 = 本地 0.6B。',
                  style: TextStyle(color: _hint, fontSize: 11, height: 1.5)),
              const SizedBox(height: 8),
              ..._tasks.map((task) => Padding(
                    padding: const EdgeInsets.symmetric(vertical: 6),
                    child: Row(children: <Widget>[
                      Expanded(flex: 4,
                          child: Text(_taskLabels[task] ?? task,
                              style: const TextStyle(
                                  color: Colors.white, fontSize: 12))),
                      Expanded(flex: 5,
                          child: Text(_routes[task]?.isEmpty ?? true
                              ? '本地 0.6B（未配置）'
                              : _routes[task]!,
                              style: TextStyle(
                                  color: (_routes[task]?.isEmpty ?? true)
                                      ? _hint : _accent, fontSize: 11))),
                      TextButton(
                        onPressed: () => unawaited(_routeDialog(task)),
                        child: const Text('配置',
                            style: TextStyle(color: _accent, fontSize: 12)),
                      ),
                    ]),
                  )),
            ]),
          ),
          // ── 本地模型（已裁撤占位）──────────────────────────────────────
          // 2026-11-12 全面云端：bge-m3 / 精排 ONNX / Qwen 0.6B 全部删除，本区块仅提示。
          Container(
            margin: const EdgeInsets.fromLTRB(16, 10, 16, 0),
            padding: const EdgeInsets.all(14),
            decoration: BoxDecoration(
              color: _panel.withValues(alpha: 0.6),
              borderRadius: BorderRadius.circular(10),
              border: Border.all(color: _border.withValues(alpha: 0.6))),
            child: Row(children: <Widget>[
              const Text('本地模型',
                  style: TextStyle(color: Colors.white, fontSize: 13,
                      fontWeight: FontWeight.w600)),
              const SizedBox(width: 12),
              Expanded(child: Text('已全部裁撤（bge-m3 / 精排 ONNX / Qwen 0.6B 已删除），'
                  '全链路走云端/号池',
                  style: const TextStyle(color: _hint, fontSize: 11))),
            ]),
          ),
        ]),
      ),
    );
  }

  Future<void> _deleteProvider(String name) async {
    try {
      await widget.client
          .call('router.provider_delete', <String, dynamic>{'name': name});
      if (!mounted) return;
      setState(() => _message = '已删除 $name（热重载完成）');
      unawaited(_load());
    } catch (error) {
      if (mounted) setState(() => _message = '删除失败：$error');
    }
  }

  /// 路由配置：下拉选择器（本地 / 号池模型 / 云供应商）。
  Future<void> _routeDialog(String task) async {
    final providers = (_data['providers'] as List<dynamic>? ?? <dynamic>[])
        .whereType<Map>()
        .map((p) => (p['name'] ?? '').toString())
        .where((n) => n.isNotEmpty)
        .toList();
    var selected = _routes[task] ?? '';
    final ok = await showDialog<bool>(
      context: context,
      builder: (BuildContext context) => StatefulBuilder(
        builder: (BuildContext context, void Function(void Function()) setDialogState) =>
            AlertDialog(
          backgroundColor: _panel,
          title: Text('配置路由：${_taskLabels[task] ?? task}',
              style: const TextStyle(color: Colors.white, fontSize: 14)),
          content: SizedBox(
            width: 380,
            child: Column(mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.start,
                children: <Widget>[
              DropdownButtonFormField<String>(
                initialValue: selected,
                dropdownColor: _panel,
                style: const TextStyle(color: Colors.white, fontSize: 12),
                decoration: const InputDecoration(
                    filled: true, fillColor: _bg,
                    enabledBorder: UnderlineInputBorder(
                        borderSide: BorderSide(color: _border))),
                items: <DropdownMenuItem<String>>[
                  const DropdownMenuItem<String>(value: '',
                      child: Text('本地 0.6B（默认）',
                          style: TextStyle(color: Colors.white))),
                  if (_poolModels.isNotEmpty) ...<DropdownMenuItem<String>>[
                    const DropdownMenuItem<String>(enabled: false,
                        child: Text('— 号池模型 —',
                            style: TextStyle(color: _hint, fontSize: 10))),
                    for (final m in _poolModels)
                      DropdownMenuItem<String>(
                          value: 'pool:${m['id']}',
                          child: Text(m['name'] as String,
                              style: const TextStyle(
                                  color: Colors.white, fontSize: 12))),
                  ],
                  if (providers.isNotEmpty) ...<DropdownMenuItem<String>>[
                    const DropdownMenuItem<String>(enabled: false,
                        child: Text('— 云供应商 —',
                            style: TextStyle(color: _hint, fontSize: 10))),
                    for (final name in providers)
                      DropdownMenuItem<String>(
                          value: 'provider:$name',
                          child: Text(name,
                              style: const TextStyle(
                                  color: Colors.white, fontSize: 12))),
                  ],
                ],
                onChanged: (value) =>
                    setDialogState(() => selected = value ?? ''),
              ),
            ]),
          ),
          actions: <Widget>[
            TextButton(onPressed: () => Navigator.of(context).pop(false),
                child: const Text('取消', style: TextStyle(color: _hint))),
            TextButton(onPressed: () => Navigator.of(context).pop(true),
                child: const Text('保存', style: TextStyle(color: _accent))),
          ],
        ),
      ),
    );
    if (ok != true) return;
    if (!mounted) return;
    try {
      final result = await widget.client.call('router.route_set',
          <String, dynamic>{'task': task, 'spec': selected});
      if (!mounted) return;
      setState(() => _message = (result is Map && result['ok'] == true)
          ? '路由已保存'
          : '保存失败：${result is Map ? (result['error'] ?? '') : ''}');
      unawaited(_load());
    } catch (error) {
      if (mounted) setState(() => _message = '保存失败：$error');
    }
  }
}
