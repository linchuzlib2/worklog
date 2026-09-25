document.addEventListener('DOMContentLoaded', () => {
  const editor = document.querySelector('#editor');
  if (!editor || typeof Quill === 'undefined') return;
  const quill = new Quill(editor, {
    theme: 'snow',
    placeholder: '写下你的内容... ',
    modules: { toolbar: [[{ header: [1, 2, 3, false] }], ['bold', 'italic', 'underline', 'strike'], [{ list: 'ordered' }, { list: 'bullet' }], ['blockquote', 'code-block', 'link'], ['clean']] }
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
  const toDateTimeValue = (date) => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}T${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
  const openScheduleDialog = (date, time) => {
    const start = new Date(`${date}T${time}:00`);
    startInput.value = toDateTimeValue(start);
    start.setMinutes(start.getMinutes() + 30);
    endInput.value = toDateTimeValue(start);
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
        fragment.append(mark);
        lastIndex = offset + match.length;
        return match;
      });
      fragment.append(text.slice(lastIndex));
      node.replaceWith(fragment);
    });
  });
}
