const SUBSCRIBER_SHEET_NAME = "line_subscribers";
const SUBSCRIBER_HEADERS = ["user_id", "display_name", "status", "first_seen_at", "last_seen_at", "source_type"];
const CONFIRMATION_TEXT = "\u5df2\u52a0\u5165\u91cd\u5927\u5373\u6642\u8a0a\u606f\u901a\u77e5\u3002\n\u4e4b\u5f8c\u6709\u65b0\u91cd\u5927\u8a0a\u606f\u6642\u6703\u901a\u77e5\u4f60\u3002";

function doPost(e) {
  // Apps Script web apps do not expose request headers to doPost, so LINE's
  // X-Line-Signature cannot be verified here. Use LINE_WEBHOOK_SECRET as a
  // shared URL token: /exec?secret=your-secret.
  if (!isAuthorized_(e)) {
    return jsonResponse_({ ok: false, error: "unauthorized" }, 403);
  }

  const body = e && e.postData && e.postData.contents ? e.postData.contents : "{}";
  const payload = JSON.parse(body);
  const events = Array.isArray(payload.events) ? payload.events : [];
  const properties = PropertiesService.getScriptProperties();
  const accessToken = properties.getProperty("LINE_CHANNEL_ACCESS_TOKEN");
  const sheetId = properties.getProperty("LINE_SUBSCRIBERS_SHEET_ID");

  if (!accessToken || !sheetId) {
    return jsonResponse_({ ok: false, error: "missing script properties" }, 500);
  }

  const lock = LockService.getScriptLock();
  lock.waitLock(10000);
  try {
    const sheet = getSubscriberSheet_(sheetId);
    events.forEach((event) => {
      const userId = event && event.source && event.source.userId ? event.source.userId : "";
      if (!userId) {
        return;
      }
      const displayName = getDisplayName_(accessToken, userId);
      upsertSubscriber_(sheet, userId, displayName, event.type || "unknown");
      if (event.replyToken) {
        replyConfirmation_(accessToken, event.replyToken);
      }
    });
  } finally {
    lock.releaseLock();
  }

  return jsonResponse_({ ok: true, handled: events.length }, 200);
}

function doGet(e) {
  if (e && e.parameter && e.parameter.health === "1") {
    return healthCheck_();
  }
  return jsonResponse_({ ok: true, message: "LINE webhook is ready." }, 200);
}

function isAuthorized_(e) {
  const expected = PropertiesService.getScriptProperties().getProperty("LINE_WEBHOOK_SECRET");
  if (!expected) {
    return true;
  }
  const actual = e && e.parameter && e.parameter.secret ? e.parameter.secret : "";
  return actual === expected;
}

function getSubscriberSheet_(sheetId) {
  const spreadsheet = SpreadsheetApp.openById(sheetId);
  let sheet = spreadsheet.getSheetByName(SUBSCRIBER_SHEET_NAME);
  if (!sheet) {
    sheet = spreadsheet.insertSheet(SUBSCRIBER_SHEET_NAME);
  }
  const firstRow = sheet.getRange(1, 1, 1, SUBSCRIBER_HEADERS.length).getValues()[0];
  const needsHeaders = firstRow.every((value) => String(value || "").trim() === "");
  if (needsHeaders) {
    sheet.getRange(1, 1, 1, SUBSCRIBER_HEADERS.length).setValues([SUBSCRIBER_HEADERS]);
    sheet.setFrozenRows(1);
  }
  return sheet;
}

function upsertSubscriber_(sheet, userId, displayName, sourceType) {
  const now = new Date();
  const rows = sheet.getDataRange().getValues();
  const userIdIndex = SUBSCRIBER_HEADERS.indexOf("user_id");
  const displayNameIndex = SUBSCRIBER_HEADERS.indexOf("display_name");
  const statusIndex = SUBSCRIBER_HEADERS.indexOf("status");
  const firstSeenIndex = SUBSCRIBER_HEADERS.indexOf("first_seen_at");
  const lastSeenIndex = SUBSCRIBER_HEADERS.indexOf("last_seen_at");
  const sourceTypeIndex = SUBSCRIBER_HEADERS.indexOf("source_type");

  for (let index = 1; index < rows.length; index += 1) {
    if (String(rows[index][userIdIndex] || "").trim() === userId) {
      const rowNumber = index + 1;
      sheet.getRange(rowNumber, displayNameIndex + 1).setValue(displayName);
      if (!String(rows[index][statusIndex] || "").trim()) {
        sheet.getRange(rowNumber, statusIndex + 1).setValue("active");
      }
      if (!rows[index][firstSeenIndex]) {
        sheet.getRange(rowNumber, firstSeenIndex + 1).setValue(now);
      }
      sheet.getRange(rowNumber, lastSeenIndex + 1).setValue(now);
      sheet.getRange(rowNumber, sourceTypeIndex + 1).setValue(sourceType);
      return;
    }
  }

  sheet.appendRow([userId, displayName, "active", now, now, sourceType]);
}

function getDisplayName_(accessToken, userId) {
  const response = UrlFetchApp.fetch(`https://api.line.me/v2/bot/profile/${encodeURIComponent(userId)}`, {
    method: "get",
    headers: {
      Authorization: `Bearer ${accessToken}`,
    },
    muteHttpExceptions: true,
  });
  if (response.getResponseCode() < 200 || response.getResponseCode() >= 300) {
    return "";
  }
  const profile = JSON.parse(response.getContentText() || "{}");
  return profile.displayName || "";
}

function replyConfirmation_(accessToken, replyToken) {
  UrlFetchApp.fetch("https://api.line.me/v2/bot/message/reply", {
    method: "post",
    contentType: "application/json",
    headers: {
      Authorization: `Bearer ${accessToken}`,
    },
    payload: JSON.stringify({
      replyToken,
      messages: [{ type: "text", text: CONFIRMATION_TEXT }],
    }),
    muteHttpExceptions: true,
  });
}

function jsonResponse_(body, statusCode) {
  const output = ContentService.createTextOutput(JSON.stringify(body));
  output.setMimeType(ContentService.MimeType.JSON);
  return output;
}

function healthCheck_() {
  const properties = PropertiesService.getScriptProperties();
  const accessToken = properties.getProperty("LINE_CHANNEL_ACCESS_TOKEN");
  const sheetId = properties.getProperty("LINE_SUBSCRIBERS_SHEET_ID");
  const webhookSecret = properties.getProperty("LINE_WEBHOOK_SECRET");
  const result = {
    ok: true,
    hasLineChannelAccessToken: Boolean(accessToken),
    hasLineSubscribersSheetId: Boolean(sheetId),
    hasLineWebhookSecret: Boolean(webhookSecret),
    canOpenSubscriberSheet: false,
    subscriberSheetIdSuffix: sheetId ? sheetId.slice(-6) : "",
  };
  if (sheetId) {
    try {
      SpreadsheetApp.openById(sheetId);
      result.canOpenSubscriberSheet = true;
    } catch (error) {
      result.ok = false;
      result.sheetError = String(error);
    }
  }
  if (!accessToken || !sheetId || !webhookSecret) {
    result.ok = false;
  }
  return jsonResponse_(result, result.ok ? 200 : 500);
}
