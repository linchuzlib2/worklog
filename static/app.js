window.chinaDateTime = (() => {
  const formatter = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23'
  });
  const pad = (value) => String(value).padStart(2, '0');
  const formatUtc = (date) => `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())}T${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}`;
  const addMinutes = (value, minutes) => {
    const [datePart, timePart] = value.split('T');
    const [year, month, day] = datePart.split('-').map(Number);
    const [hour, minute] = timePart.split(':').map(Number);
    return formatUtc(new Date(Date.UTC(year, month - 1, day, hour, minute + minutes)));
  };
  return {
    nowValue() {
      const parts = Object.fromEntries(formatter.formatToParts(new Date()).map(({ type, value }) => [type, value]));
      return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
    },
    addMinutes,
    nextHalfHour() {
      const current = this.nowValue();
      const minute = Number(current.slice(-2));
      return addMinutes(current, minute < 30 ? 30 - minute : 60 - minute);
    }
  };
})();

document.addEventListener('DOMContentLoaded', () => {
  const editor = document.querySelector('#editor');
  if (!editor || typeof Quill === 'undefined') return;
  const quill = new Quill(editor, {
    theme: 'snow',
    placeholder: '写下你的内容... ',
    modules: {
      toolbar: {
        container: [
          [{ header: [1, 2, 3, false] }],
          ['bold', 'italic', 'underline', 'strike'],
          [{ list: 'ordered' }, { list: 'bullet' }],
          ['blockquote', 'code-block', 'link'],
          ['image'],
          ['clean']
        ],
        handlers: {
          image: function () {
            const input = document.createElement('input');
            input.setAttribute('type', 'file');
            input.setAttribute('accept', '.png,.jpg,.jpeg,.gif,.webp,.bmp,.svg,.ico,image/*');
            input.style.display = 'none';
            input.addEventListener('change', async () => {
              if (!input.files || !input.files.length) {
                document.body.removeChild(input);
                return;
              }
              const file = input.files[0];
              const fileName = (file.name || '').toLowerCase();
              const isSupportedImage = file.type.startsWith('image/') || /\.(png|jpe?g|gif|webp|bmp|svg|ico)$/i.test(fileName);
              if (!isSupportedImage) {
                alert('仅支持 PNG、JPG/JPEG、GIF、WEBP、BMP、SVG、ICO 等图片格式');
                document.body.removeChild(input);
                return;
              }
              if (file.size > 25 * 1024 * 1024) {
                alert('图片不能超过 25 MB');
                document.body.removeChild(input);
                return;
              }
              const range = quill.getSelection(true) || { index: 0, length: 0 };
              const placeholder = '图片上传中…';
              quill.disable();
              quill.insertText(range.index, placeholder);
              try {
                const base64 = await new Promise((resolve, reject) => {
                  const reader = new FileReader();
                  reader.onload = () => resolve(reader.result);
                  reader.onerror = () => reject(new Error('图片读取失败'));
                  reader.readAsDataURL(file);
                });
                quill.deleteText(range.index, placeholder.length);
                quill.insertEmbed(range.index, 'image', base64, 'api');
                quill.setSelection(range.index + 1, 0);
              } catch (error) {
                quill.deleteText(range.index, placeholder.length);
                alert('图片上传失败：' + error.message);
              } finally {
                quill.enable();
                document.body.removeChild(input);
              }
            });
            document.body.appendChild(input);
            input.click();
          }
        }
      }
    }
  });
  const form = editor.closest('form');
  form.addEventListener('submit', () => {
    const target = form.querySelector('input[type="hidden"][name="description"], input[type="hidden"][name="content"]');
    target.value = quill.root.innerHTML;
  });
});

document.querySelectorAll('.file-card[draggable="true"]').forEach((file) => {
  file.addEventListener('dragstart', (event) => {
    event.dataTransfer.setData('text/plain', file.dataset.fileId);
    file.classList.add('dragging');
  });
  file.addEventListener('dragend', () => file.classList.remove('dragging'));
});
document.querySelectorAll('.folder-card[data-folder-id], .tree-folder[data-folder-id]').forEach((folder) => {
  folder.addEventListener('dragover', (event) => { event.preventDefault(); event.stopPropagation(); folder.classList.add('drop-target'); });
  folder.addEventListener('dragleave', () => folder.classList.remove('drop-target'));
  folder.addEventListener('drop', async (event) => {
    event.preventDefault();
    event.stopPropagation();
    folder.classList.remove('drop-target');
    const fileId = event.dataTransfer.getData('text/plain');
    const response = await fetch(`/files/${fileId}/move`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ folder_id: folder.dataset.folderId }) });
    if (response.ok) window.location.reload();
  });
});

document.querySelectorAll('[data-rename]').forEach((button) => {
  button.addEventListener('click', () => {
    const item = button.closest('.tree-folder');
    const form = item.querySelector(':scope > .rename-form');
    form.hidden = false;
    const input = form.querySelector('input');
    input.focus();
    input.select();
  });
});

document.querySelectorAll('.tree-toggle:not(.blank)').forEach((toggle) => {
  toggle.addEventListener('click', () => {
    toggle.closest('.tree-folder').classList.toggle('open');
  });
});

document.querySelectorAll('.duplicate-aware-upload').forEach((form) => {
  form.addEventListener('submit', async (event) => {
    const input = form.querySelector('input[type="file"]');
    const allow = form.querySelector('input[name="allow_duplicate"]');
    if (!input.files.length || allow.value === '1') return;
    event.preventDefault();
    const response = await fetch(`/attachments/check-name?filename=${encodeURIComponent(input.files[0].name)}`);
    const result = await response.json();
    if (!result.duplicate || confirm(`文件管理中已有“${input.files[0].name}”，仍要重复上传吗？`)) {
      allow.value = '1';
      form.submit();
    }
  });
});

document.addEventListener('keydown', (event) => {
  if (!(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== 's') return;
  const form = document.querySelector('form.editor-form, form.duplicate-aware-upload');
  if (!form) return;
  event.preventDefault();
  form.requestSubmit();
});

const scheduleDialog = document.querySelector('#schedule-dialog');
if (scheduleDialog) {
  const startInput = scheduleDialog.querySelector('input[name="start_at"]');
  const endInput = scheduleDialog.querySelector('input[name="end_at"]');
  const syncEndFromStart = () => {
    if (!startInput.value) return;
    const suggestedEnd = window.chinaDateTime.addMinutes(startInput.value, 30);
    if (!endInput.value || endInput.value <= startInput.value) {
      endInput.value = suggestedEnd;
    }
  };
  ['input', 'change', 'blur'].forEach((eventName) => {
    startInput.addEventListener(eventName, syncEndFromStart);
  });
  const openScheduleDialog = (date, time) => {
    startInput.value = `${date}T${time}`;
    endInput.value = window.chinaDateTime.addMinutes(startInput.value, 30);
    scheduleDialog.showModal();
    scheduleDialog.querySelector('input[name="title"]').focus();
  };
  document.querySelectorAll('.calendar-slot, .calendar-day-header').forEach((cell) => {
    cell.addEventListener('click', (event) => {
      if (event.target.closest('.schedule-card, a, form, button')) return;
      openScheduleDialog(cell.dataset.date, cell.dataset.time || '09:00');
    });
    cell.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      event.preventDefault();
      openScheduleDialog(cell.dataset.date, cell.dataset.time || '09:00');
    });
  });
  scheduleDialog.querySelector('[data-close-schedule]').addEventListener('click', () => scheduleDialog.close());
  scheduleDialog.addEventListener('click', (event) => {
    if (event.target === scheduleDialog) scheduleDialog.close();
  });
}

const highlightRoot = document.querySelector('[data-search-query]');
const highlightQuery = highlightRoot?.dataset.searchQuery || new URLSearchParams(window.location.search).get('q') || '';
if (highlightQuery.trim()) {
  const escapedQuery = highlightQuery.trim().replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const matcher = new RegExp(escapedQuery, 'gi');
  let firstMark = null;
  document.querySelectorAll('[data-highlight]').forEach((container) => {
    const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, {
      acceptNode: (node) => node.parentElement?.closest('mark, script, style') ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT
    });
    const textNodes = [];
    while (walker.nextNode()) textNodes.push(walker.currentNode);
    textNodes.forEach((node) => {
      const text = node.nodeValue;
      matcher.lastIndex = 0;
      if (!matcher.test(text)) return;
      matcher.lastIndex = 0;
      const fragment = document.createDocumentFragment();
      let lastIndex = 0;
      text.replace(matcher, (match, offset) => {
        fragment.append(text.slice(lastIndex, offset));
        const mark = document.createElement('mark');
        mark.textContent = match;
        if (!firstMark) firstMark = mark;
        fragment.append(mark);
        lastIndex = offset + match.length;
        return match;
      });
      fragment.append(text.slice(lastIndex));
      node.replaceWith(fragment);
    });
  });
  // 跳到搜索结果中第一个关键字的位置
  if (firstMark) {
    firstMark.id = 'first-match';
    firstMark.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
}
