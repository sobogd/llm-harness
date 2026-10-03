import 'package:flutter/material.dart';

import '../model.dart';
import '../state.dart';
import 'detail.dart';
import 'sessions.dart';
import 'settings.dart';

class RootScreen extends StatefulWidget {
  final AgentApp app;
  const RootScreen({super.key, required this.app});

  @override
  State<RootScreen> createState() => _RootScreenState();
}

class _Observer extends NavigatorObserver {
  final void Function(Route route) onPush;
  final void Function(Route route) onPop;
  _Observer(this.onPush, this.onPop);

  @override
  void didPush(Route route, Route? previous) => onPush(route);

  @override
  void didPop(Route route, Route? previous) => onPop(route);
}

class _RootScreenState extends State<RootScreen> {
  final GlobalKey<NavigatorState> _nav = GlobalKey();
  final ValueNotifier<String?> _selected = ValueNotifier(null);
  bool _detailOpen = false;
  bool _narrow = true;
  late final _Observer _obs = _Observer(_onPush, _onPop);

  @override
  void dispose() {
    _selected.dispose();
    super.dispose();
  }

  void _onPush(Route route) {
    _detailOpen = route.settings.name == '/detail';
  }

  void _onPop(Route route) {
    if (route.settings.name != '/detail') return;
    _detailOpen = false;
    if (mounted && _selected.value != null) {
      setState(() => _selected.value = null);
    }
  }

  void _select(String id, {required bool push}) {
    if (id == _selected.value) return;
    setState(() => _selected.value = id);
    final a = widget.app;
    final s = a.sessions.firstWhere(
      (e) => e.id == id,
      orElse: () => throw StateError('no session $id'),
    );
    if (!s.active) a.loadSession(id);
    if (push && !_detailOpen) {
      _detailOpen = true;
      _nav.currentState?.pushNamed('/detail');
    }
  }

  Future<void> _new() async {
    final a = widget.app;
    if (a.busy) return;
    await a.newSession();
    if (!mounted) return;
    SessionInfo? s;
    for (final e in a.sessions) {
      if (e.active) {
        s = e;
        break;
      }
    }
    if (s == null) return;
    final id = s.id;
    if (id == _selected.value) return;
    setState(() => _selected.value = id);
    if (!_detailOpen && _narrow) {
      _detailOpen = true;
      _nav.currentState?.pushNamed('/detail');
    }
  }

  void _delete(SessionInfo s) {
    if (s.id != _selected.value) return;
    setState(() => _selected.value = null);
    if (_narrow && _detailOpen) {
      _detailOpen = false;
      _nav.currentState?.pop();
    }
  }

  Route? _onGenerateRoute(RouteSettings rs) {
    if (rs.name == '/detail' && _selected.value != null) {
      return MaterialPageRoute(
        settings: rs,
        builder: (_) => SessionDetailScreen(app: widget.app),
      );
    }
    return null;
  }

  Widget _narrowNav() => Navigator(
        key: _nav,
        onGenerateRoute: _onGenerateRoute,
        observers: [_obs],
        onGenerateInitialRoutes: (nav, name) {
          final routes = <Route>[
            MaterialPageRoute(builder: (_) => _listScreen()),
          ];
          if (_selected.value != null) {
            routes.add(
              MaterialPageRoute(
                settings: const RouteSettings(name: '/detail'),
                builder: (_) => SessionDetailScreen(app: widget.app),
              ),
            );
          }
          return routes;
        },
      );

  Widget _listScreen() => SessionsListScreen(
        app: widget.app,
        selected: _selected,
        onOpen: (id) => _select(id, push: true),
        onNew: _new,
        onSettings: () => Navigator.of(context).push(
          MaterialPageRoute(builder: (_) => SettingsScreen(app: widget.app)),
        ),
        onDeleted: _delete,
      );

  Widget _wide() => Scaffold(
        body: SafeArea(
          child: Row(
            children: [
              SizedBox(
                width: 360,
                child: Column(
                  children: [
                    Padding(
                      padding: const EdgeInsets.symmetric(
                        horizontal: 12,
                        vertical: 8,
                      ),
                      child: Row(
                        children: [
                          const Text(
                            'harness',
                            style: TextStyle(
                              fontWeight: FontWeight.w600,
                              fontSize: 16,
                            ),
                          ),
                          const Spacer(),
                          TextButton.icon(
                            onPressed: widget.app.busy ? null : _new,
                            icon: const Icon(Icons.add, size: 18),
                            label: const Text('новая'),
                          ),
                          IconButton(
                            icon: const Icon(Icons.settings),
                            tooltip: 'настройки',
                            onPressed: () => Navigator.of(context).push(
                              MaterialPageRoute(
                                builder: (_) =>
                                    SettingsScreen(app: widget.app),
                              ),
                            ),
                          ),
                        ],
                      ),
                    ),
                    const Divider(height: 12),
                    Expanded(
                      child: SessionsListBody(
                        app: widget.app,
                        selected: _selected,
                        onOpen: (id) => _select(id, push: false),
                        onDeleted: _delete,
                      ),
                    ),
                  ],
                ),
              ),
              const VerticalDivider(width: 1),
              Expanded(
                child: _selected.value == null
                    ? const Center(child: Text('выберите сессию'))
                    : ChatPane(app: widget.app),
              ),
            ],
          ),
        ),
      );

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: widget.app,
      builder: (context, _) => LayoutBuilder(
        builder: (context, c) {
          final wide = c.maxWidth >= 900;
          final narrow = !wide;
          if (narrow != _narrow) {
            _narrow = narrow;
            _detailOpen = _selected.value != null;
          }
          return wide ? _wide() : _narrowNav();
        },
      ),
    );
  }
}
