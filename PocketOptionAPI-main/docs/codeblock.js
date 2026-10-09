// Add copy button to all code blocks
document.addEventListener('DOMContentLoaded', function() {
  document.querySelectorAll('pre code').forEach(function(codeBlock) {
    var pre = codeBlock.parentNode;
    var button = document.createElement('button');
    button.className = 'copy-btn';
    button.type = 'button';
    button.innerText = 'Copy';
    button.onclick = function() {
      var text = codeBlock.innerText;
      navigator.clipboard.writeText(text).then(function() {
        button.innerText = 'Copied!';
        setTimeout(function() { button.innerText = 'Copy'; }, 1200);
      });
    };
    pre.appendChild(button);
  });
});
