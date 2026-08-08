document.addEventListener("DOMContentLoaded", () => {
    // コードブロックをクリックでコピーする
    document.querySelectorAll("pre").forEach((block) => {
        block.style.cursor = "pointer";
        block.title = "クリックでコピー";
        block.addEventListener("click", async () => {
            try {
                await navigator.clipboard.writeText(block.innerText);
                const prev = block.dataset.label || "";
                block.dataset.label = prev;
                block.style.outline = "2px solid var(--accent)";
                setTimeout(() => {
                    block.style.outline = "";
                }, 800);
            } catch (_) {
                // クリップボード非対応時は何もしない
            }
        });
    });
});
