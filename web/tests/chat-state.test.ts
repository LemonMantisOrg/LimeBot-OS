import test from "node:test";
import { strict as assert } from "node:assert";

import {
  applyUserMessageEdit,
  applyFinalAssistantMessage,
  applyStreamSnapshot,
  applyStopTyping,
  getUserTurnIndex,
  upsertChangeSet,
  upsertToolExecution,
  upsertStreamDelta,
  type ChatMessage,
} from "../src/lib/chat-state.js";

test("stream deltas and final message target the same bot bubble by message_id", () => {
  const initial: ChatMessage[] = [
    { sender: "user", content: "ok do it" },
    { sender: "bot", content: "", isStreaming: true, messageId: "msg-1", turnId: "turn-1" },
  ];

  const streamed = upsertStreamDelta(initial, {
    messageId: "msg-1",
    turnId: "turn-1",
    contentDelta: "Checking changelog...",
  });
  const finalized = applyFinalAssistantMessage(streamed, {
    messageId: "msg-1",
    turnId: "turn-1",
    content: "Checked the changelog.",
    variant: "default",
  });

  assert.equal(finalized.length, 2);
  assert.equal(finalized[1].content, "Checked the changelog.");
  assert.equal(finalized[1].isStreaming, false);
  assert.equal(finalized[1].messageId, "msg-1");
});

test("final replies do not overwrite an older completed bot message when ids differ", () => {
  const initial: ChatMessage[] = [
    { sender: "bot", content: "Old completed reply", isStreaming: false, messageId: "msg-old", turnId: "turn-old" },
    { sender: "user", content: "next task" },
    { sender: "bot", content: "Working", isStreaming: true, messageId: "msg-new", turnId: "turn-new" },
  ];

  const finalized = applyFinalAssistantMessage(initial, {
    messageId: "msg-new",
    turnId: "turn-new",
    content: "Fresh final reply",
    variant: "default",
  });

  assert.equal(finalized.length, 3);
  assert.equal(finalized[0].content, "Old completed reply");
  assert.equal(finalized[2].content, "Fresh final reply");
  assert.equal(finalized[2].messageId, "msg-new");
});

test("stop_typing only clears the targeted streaming assistant message", () => {
  const initial: ChatMessage[] = [
    { sender: "bot", content: "Older stream", isStreaming: true, messageId: "msg-old", turnId: "turn-old" },
    { sender: "bot", content: "Current stream", isStreaming: true, messageId: "msg-now", turnId: "turn-now" },
  ];

  const stopped = applyStopTyping(initial, { messageId: "msg-now", turnId: "turn-now" });

  assert.equal(stopped[0].isStreaming, true);
  assert.equal(stopped[1].isStreaming, false);
});

test("post-tool stream reopens the stopped assistant bubble", () => {
  const initial: ChatMessage[] = [
    { sender: "bot", content: "Planning...", isStreaming: false, messageId: "msg-1", turnId: "turn-1" },
  ];

  const streamed = upsertStreamDelta(initial, {
    messageId: "msg-1",
    turnId: "turn-1",
    contentDelta: "Final answer",
  });

  assert.equal(streamed.length, 1);
  assert.equal(streamed[0].content, "Planning...Final answer");
  assert.equal(streamed[0].isStreaming, true);
});

test("intermediate full-content snapshots stay streamable", () => {
  const initial: ChatMessage[] = [
    { sender: "bot", content: "tool residue", isStreaming: true, messageId: "msg-2", turnId: "turn-2" },
  ];

  const snapshot = applyStreamSnapshot(initial, {
    messageId: "msg-2",
    turnId: "turn-2",
    content: "",
  });

  assert.equal(snapshot.length, 1);
  assert.equal(snapshot[0].content, "");
  assert.equal(snapshot[0].isStreaming, true);
});

test("targeted stop_typing does not stop an unrelated stream", () => {
  const initial: ChatMessage[] = [
    { sender: "bot", content: "Current", isStreaming: true, messageId: "msg-current", turnId: "turn-current" },
  ];

  const stopped = applyStopTyping(initial, { messageId: "msg-old", turnId: "turn-old" });

  assert.equal(stopped[0].isStreaming, true);
});

test("late tool execution is inserted before the final reply for the same turn", () => {
  const initial: ChatMessage[] = [
    { sender: "user", content: "make an image" },
    {
      sender: "bot",
      type: "text",
      content: "The image model failed.",
      isStreaming: false,
      messageId: "msg-final",
      turnId: "turn-image",
    },
  ];

  const updated = upsertToolExecution(initial, {
    turnId: "turn-image",
    toolExecution: {
      tool: "generate_image",
      status: "error",
      args: { prompt: "guinea pig" },
      result: "model failed",
      tool_call_id: "tool-1",
    },
  });

  assert.equal(updated.length, 3);
  assert.equal(updated[1].type, "tool");
  assert.equal(updated[1].toolExecution?.tool, "generate_image");
  assert.equal(updated[2].content, "The image model failed.");
});

test("updated tool execution is moved before an existing final reply for the same turn", () => {
  const initial: ChatMessage[] = [
    { sender: "user", content: "make an image" },
    {
      sender: "bot",
      type: "text",
      content: "The image model failed.",
      isStreaming: false,
      messageId: "msg-final",
      turnId: "turn-image",
    },
    {
      sender: "bot",
      type: "tool",
      content: "",
      turnId: "turn-image",
      toolExecution: {
        tool: "generate_image",
        status: "running",
        args: { prompt: "guinea pig" },
        tool_call_id: "tool-1",
      },
    },
  ];

  const updated = upsertToolExecution(initial, {
    turnId: "turn-image",
    toolExecution: {
      tool: "generate_image",
      status: "error",
      args: { prompt: "guinea pig" },
      result: "model failed",
      tool_call_id: "tool-1",
    },
  });

  assert.equal(updated[1].type, "tool");
  assert.equal(updated[1].toolExecution?.status, "error");
  assert.equal(updated[2].content, "The image model failed.");
});

test("final replies replace a stopped streaming bubble for the same turn", () => {
  const initial: ChatMessage[] = [
    { sender: "bot", content: "Saving...", isStreaming: true, turnId: "turn-save" },
  ];

  const stopped = applyStopTyping(initial, { turnId: "turn-save" });
  const finalized = applyFinalAssistantMessage(stopped, {
    turnId: "turn-save",
    content: "Saved once.",
    variant: "default",
  });

  assert.equal(finalized.length, 1);
  assert.equal(finalized[0].content, "Saved once.");
  assert.equal(finalized[0].isStreaming, false);
});

test("final replies trim repeated sections before rendering", () => {
  const repeated = [
    "Saved, baby.",
    "I'll remember that 244069957187534848 is you on Discord.",
    "Saved, baby.",
    "I'll remember that 244069957187534848 is you on Discord.",
  ].join("\n\n");

  const finalized = applyFinalAssistantMessage([], {
    turnId: "turn-memory",
    content: repeated,
    variant: "default",
  });

  assert.equal(finalized.length, 1);
  assert.equal(
    finalized[0].content,
    ["Saved, baby.", "I'll remember that 244069957187534848 is you on Discord."].join(
      "\n\n"
    )
  );
});

test("user edit keeps the edited bubble and drops later turns", () => {
  const initial: ChatMessage[] = [
    { sender: "user", content: "first", messageId: "usr-1" },
    { sender: "bot", content: "reply one", turnId: "turn-1" },
    { sender: "user", content: "second", messageId: "usr-2" },
    { sender: "bot", content: "reply two", turnId: "turn-2" },
  ];

  const updated = applyUserMessageEdit(
    initial,
    "usr-2",
    "second, but better",
    "usr-9"
  );

  assert.equal(updated.length, 3);
  assert.equal(updated[2].sender, "user");
  assert.equal(updated[2].content, "second, but better");
  assert.equal(updated[2].messageId, "usr-9");
});

test("user turn index counts only user messages", () => {
  const initial: ChatMessage[] = [
    { sender: "user", content: "first", messageId: "usr-1" },
    { sender: "bot", content: "reply one", turnId: "turn-1" },
    { sender: "user", content: "second", messageId: "usr-2" },
  ];

  assert.equal(getUserTurnIndex(initial, "usr-1"), 0);
  assert.equal(getUserTurnIndex(initial, "usr-2"), 1);
  assert.equal(getUserTurnIndex(initial, "missing"), -1);
});

test("change set updates survive out-of-order progress and retain the terminal state", () => {
  const planned = upsertChangeSet([], {
    turnId: "turn-review",
    changeSet: {
      id: "changeset-1",
      status: "awaiting_approval",
      summary: "One file staged",
      changed_files: [{ file_id: "file-1", added: 2, removed: 1 }],
    },
  });
  const verified = upsertChangeSet(planned, {
    turnId: "turn-review",
    changeSet: {
      id: "changeset-1",
      status: "verified",
      summary: "One file staged",
      verification: [{ id: "verification-1", label: "Tests", status: "passed" }],
    },
  });
  const staleProgress = upsertChangeSet(verified, {
    turnId: "turn-review",
    changeSet: {
      id: "changeset-1",
      status: "applied",
      summary: "One file staged",
    },
  });

  assert.equal(staleProgress.length, 1);
  assert.equal(staleProgress[0].changeSet?.status, "verified");
});

test("send_media image envelope merges into the same assistant bubble", () => {
  const initial: ChatMessage[] = [
    {
      sender: "bot",
      content: "Here you go.",
      isStreaming: false,
      messageId: "msg-media",
      turnId: "turn-media",
    },
  ];

  const withImage = applyFinalAssistantMessage(initial, {
    messageId: "msg-media",
    turnId: "turn-media",
    content: "Rosé",
    variant: "default",
    image: "/temp/downloads/rose.jpg",
    attachments: [
      {
        name: "rose.jpg",
        mimeType: "image/jpeg",
        kind: "image",
        url: "/temp/downloads/rose.jpg",
      },
    ],
  });

  assert.equal(withImage.length, 1);
  assert.equal(withImage[0].content, "Here you go.");
  assert.equal(withImage[0].image, "/temp/downloads/rose.jpg");
  assert.equal(withImage[0].attachments?.[0].kind, "image");
  assert.equal(withImage[0].attachments?.[0].url, "/temp/downloads/rose.jpg");
});
