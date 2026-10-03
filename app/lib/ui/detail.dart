import 'package:flutter/material.dart';

import '../state.dart';
import 'chat.dart';

class ChatPane extends StatelessWidget {
  final AgentApp app;
  const ChatPane({super.key, required this.app});

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: app,
      builder: (context, _) => SafeArea(
        top: false,
        child: Column(
          children: [
            ChatActions(app: app),
            ChatBanner(app: app),
            if (app.toast != null)
              Container(
                width: double.infinity,
                color: Theme.of(context)
                    .colorScheme
                    .surfaceContainerHighest,
                padding: const EdgeInsets.symmetric(
                  horizontal: 16,
                  vertical: 6,
                ),
                child: Text(
                  app.toast!,
                  style: Theme.of(context).textTheme.labelMedium,
                ),
              ),
            Expanded(
              child: app.messages.isEmpty && app.live == null
                  ? const Center(child: Text('напишите сообщение'))
                  : MessageList(app: app),
            ),
            InputBar(app: app),
          ],
        ),
      ),
    );
  }
}

class SessionDetailScreen extends StatelessWidget {
  final AgentApp app;
  const SessionDetailScreen({super.key, required this.app});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('harness'), centerTitle: false),
      body: ChatPane(app: app),
    );
  }
}