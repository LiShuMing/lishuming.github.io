(function () {
  'use strict';

  function activateNotebook(container) {
    var button = container.querySelector('.notebook-embed__toggle');
    var frameContainer = container.querySelector('.notebook-embed__frame');
    var frame = frameContainer && frameContainer.querySelector('iframe');

    if (!button || !frameContainer || !frame) {
      return;
    }

    button.addEventListener('click', function () {
      var expanded = button.getAttribute('aria-expanded') === 'true';

      if (!frame.src) {
        frame.src = frame.getAttribute('data-src');
      }

      frameContainer.hidden = expanded;
      button.setAttribute('aria-expanded', String(!expanded));
      button.textContent = expanded ? '在文章内加载 Notebook' : '收起 Notebook';
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    Array.prototype.forEach.call(
      document.querySelectorAll('[data-notebook-embed]'),
      activateNotebook
    );
  });
}());
