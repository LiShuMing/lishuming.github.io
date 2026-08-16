(function () {
  'use strict';

  var pythonKeywords = new Set([
    'False', 'None', 'True', 'and', 'as', 'assert', 'async', 'await',
    'break', 'case', 'class', 'continue', 'def', 'del', 'elif', 'else',
    'except', 'finally', 'for', 'from', 'global', 'if', 'import', 'in',
    'is', 'lambda', 'match', 'nonlocal', 'not', 'or', 'pass', 'raise',
    'return', 'try', 'type', 'while', 'with', 'yield'
  ]);
  var identifierPattern = /[A-Za-z_][A-Za-z0-9_]*/g;

  function previousNonSpace(text, offset) {
    var prefix = text.slice(0, offset).replace(/\s+$/, '');
    return prefix.charAt(prefix.length - 1);
  }

  function nextNonSpace(text, offset) {
    return text.slice(offset).replace(/^\s+/, '').charAt(0);
  }

  function classifyIdentifier(lineText, start, end) {
    var before = previousNonSpace(lineText, start);
    var after = nextNonSpace(lineText, end);

    if (before === '.') {
      return after === '(' ? 'code-token-method' : 'code-token-member';
    }
    if (after === '(') {
      return 'code-token-call';
    }
    return 'code-token-variable';
  }

  function enhanceTextNode(node, lineText, lineOffset) {
    var text = node.nodeValue;
    var matches = [];
    var match;

    identifierPattern.lastIndex = 0;
    while ((match = identifierPattern.exec(text)) !== null) {
      if (pythonKeywords.has(match[0])) {
        continue;
      }
      matches.push({
        start: match.index,
        end: match.index + match[0].length,
        value: match[0],
        className: classifyIdentifier(
          lineText,
          lineOffset + match.index,
          lineOffset + match.index + match[0].length
        )
      });
    }

    if (!matches.length) {
      return;
    }

    var fragment = document.createDocumentFragment();
    var cursor = 0;

    matches.forEach(function (item) {
      fragment.appendChild(document.createTextNode(text.slice(cursor, item.start)));

      var token = document.createElement('span');
      token.className = item.className;
      token.textContent = item.value;
      fragment.appendChild(token);
      cursor = item.end;
    });

    fragment.appendChild(document.createTextNode(text.slice(cursor)));
    node.parentNode.replaceChild(fragment, node);
  }

  function enhanceLine(line) {
    var content = line.lastElementChild;
    if (!content) {
      return;
    }

    var lineText = content.textContent;
    var textNodes = [];
    var offset = 0;

    Array.prototype.forEach.call(content.childNodes, function (child) {
      var childText = child.textContent || '';
      if (child.nodeType === Node.TEXT_NODE) {
        textNodes.push({ node: child, offset: offset });
      }
      offset += childText.length;
    });

    textNodes.forEach(function (item) {
      enhanceTextNode(item.node, lineText, item.offset);
    });
  }

  function enhancePythonBlock(code) {
    Array.prototype.forEach.call(code.children, enhanceLine);
  }

  document.addEventListener('DOMContentLoaded', function () {
    var blocks = document.querySelectorAll('.highlight code.language-python');
    Array.prototype.forEach.call(blocks, enhancePythonBlock);
  });
}());
