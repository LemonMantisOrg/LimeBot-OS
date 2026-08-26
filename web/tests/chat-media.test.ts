import test from "node:test";
import { strict as assert } from "node:assert";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { ChatImage } from "../src/components/chat/ChatImage.js";
import {
  messageHasInlineImage,
  normalizeIncomingAttachments,
  renderableAttachmentsForMessage,
  resolveAttachmentUrl,
} from "../src/lib/chat-media.js";

test("metadata.image and attachments produce a renderable image src", () => {
  const attachments = normalizeIncomingAttachments([
    {
      name: "rose.jpg",
      mime_type: "image/jpeg",
      kind: "image",
      url: "/temp/downloads/rose.jpg",
    },
  ]);

  const message = {
    image: "/temp/downloads/rose.jpg",
    attachments,
  };

  assert.equal(messageHasInlineImage(message), true);
  const renderable = renderableAttachmentsForMessage(message);
  assert.equal(renderable.length, 1);
  assert.equal(renderable[0].kind, "image");
  assert.equal(resolveAttachmentUrl(renderable[0].url), "/temp/downloads/rose.jpg");
});

test("a message with metadata.image renders an img tag", () => {
  const html = renderToStaticMarkup(
    createElement(ChatImage, {
      src: "/temp/downloads/rose.jpg",
      alt: "Rosé of BLACKPINK",
    }),
  );

  assert.match(html, /<img\b/);
  assert.match(html, /src="\/temp\/downloads\/rose\.jpg"/);
  assert.match(html, /alt="Rosé of BLACKPINK"/);
});
