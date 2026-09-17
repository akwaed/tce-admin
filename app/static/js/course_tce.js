document.addEventListener('DOMContentLoaded', () => {
    const form = document.getElementById('tce-edit-form');
    if (!form) return;
    const dateActions = [...form.querySelectorAll('[data-tce-date]')];
    const flagAction = document.getElementById('tce_flag_action');
    const checkboxes = [...document.querySelectorAll('.tce-course-select')];
    const selectAll = document.getElementById('tce-select-page');
    const count = document.getElementById('tce-selection-count');
    const save = document.getElementById('tce-save');

    function refresh() {
        dateActions.forEach(select => {
            const input = document.getElementById(select.dataset.tceDate);
            input.disabled = select.value !== 'set';
            input.required = select.value === 'set';
        });
        const selected = checkboxes.filter(input => input.checked).length;
        if (count) count.textContent = `${selected} course${selected === 1 ? '' : 's'} selected on this page.`;
        if (selectAll) {
            selectAll.checked = checkboxes.length > 0 && selected === checkboxes.length;
            selectAll.indeterminate = selected > 0 && selected < checkboxes.length;
        }
        const changed = dateActions.some(select => select.value !== 'keep') || flagAction.value !== 'keep';
        save.disabled = !changed || (count !== null && selected === 0);
    }

    [...dateActions, flagAction, ...checkboxes].forEach(input => input.addEventListener('change', refresh));
    selectAll?.addEventListener('change', () => {
        checkboxes.forEach(input => { input.checked = selectAll.checked; });
        refresh();
    });
    window.addEventListener('pageshow', refresh);
    refresh();
});
