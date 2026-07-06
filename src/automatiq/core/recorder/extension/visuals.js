(function() {
    if (window._zenVisualsLoaded) return;
    window._zenVisualsLoaded = true;

    const config = {
        size: 60, color: '#fde047', opacity: 0.4, rayIntensity: 5, ghostingIntensity: 0.2,
        clickColor: '#22c55e', rippleThickness: 3, rippleExpansion: 1.2
    };

    let mouseX = window.innerWidth / 2, mouseY = window.innerHeight / 2;
    let ghostX = mouseX, ghostY = mouseY;
    let isClicking = false, isScrolling = false, scrollTimeout = null;
    let typedString = '', typeTimeout = null, activeModifiers = [];

    function initUI() {
        if (document.getElementById('zen-recording-container')) return;

        const container = document.createElement('div');
        container.id = 'zen-recording-container';
        Object.assign(container.style, {
            position: 'fixed', top: '0', left: '0', width: '100vw', height: '100vh',
            pointerEvents: 'none', zIndex: '2147483647', display: 'block', overflow: 'hidden'
        });

        const shadow = container.attachShadow({ mode: 'open' });
        const root = document.createElement('div');
        shadow.appendChild(root);

        const style = document.createElement('style');
        style.appendChild(document.createTextNode(`
            .highlighter { position: absolute; border-radius: 50%; pointer-events: none !important; transform-origin: center; will-change: transform, left, top; transition: opacity 0.2s ease, background-color 0.2s ease, box-shadow 0.2s ease, transform 0.15s cubic-bezier(0.175, 0.885, 0.32, 1.275); }
            .ripple { position: absolute; border-radius: 50%; pointer-events: none !important; border-style: solid; transform: translate(-50%, -50%); animation: ripple-scale 0.7s cubic-bezier(0.19, 1, 0.22, 1) forwards; }
            @keyframes ripple-scale { 0% { transform: translate(-50%, -50%) scale(0.2); opacity: 1; } 100% { transform: translate(-50%, -50%) scale(1.8); opacity: 0; } }
            .typing-hud { position: absolute; display: flex; flex-direction: column; align-items: center; gap: 6px; transform: translateX(-50%); font-family: -apple-system, system-ui, sans-serif; pointer-events: none !important; }
            .modifier-row { display: flex; gap: 4px; pointer-events: none !important; }
            .pill { background: #000; color: #fff; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 800; text-transform: uppercase; border: 1px solid rgba(255,255,255,0.2); box-shadow: 0 4px 8px rgba(0,0,0,0.3); pointer-events: none !important; }
            .typing-text { background: rgba(0, 0, 0, 0.85); color: #4ade80; padding: 6px 12px; border-radius: 6px; font-size: 16px; font-weight: bold; font-family: monospace; border: 1px solid rgba(255,255,255,0.2); box-shadow: 0 4px 8px rgba(0,0,0,0.3); white-space: pre; opacity: 0; transition: opacity 0.2s ease; pointer-events: none !important; }
        `));
        shadow.appendChild(style);
        document.documentElement.appendChild(container);

        const highlighter = document.createElement('div');
        highlighter.className = 'highlighter';
        root.appendChild(highlighter);

        const hud = document.createElement('div');
        hud.className = 'typing-hud';
        const modRow = document.createElement('div');
        modRow.className = 'modifier-row';
        const textDisplay = document.createElement('div');
        textDisplay.className = 'typing-text';
        hud.appendChild(modRow);
        hud.appendChild(textDisplay);
        root.appendChild(hud);

        function createRipple(x, y) {
            const r = document.createElement('div');
            const size = config.size * config.rippleExpansion;
            r.className = 'ripple';
            Object.assign(r.style, {
                left: x+'px', top: y+'px', width: size+'px', height: size+'px',
                borderColor: config.clickColor, borderWidth: config.rippleThickness+'px', opacity: 0.8
            });
            root.appendChild(r);
            setTimeout(() => r.remove(), 800);
        }

        function loop() {
            const factor = 0.4 - (config.ghostingIntensity * 0.35);
            ghostX += (mouseX - ghostX) * factor;
            ghostY += (mouseY - ghostY) * factor;

            const activeColor = isClicking ? config.clickColor : config.color;
            let scale = isClicking ? 0.8 : isScrolling ? 1.25 : 1.0;

            Object.assign(highlighter.style, {
                left: (ghostX - config.size / 2) + 'px', top: (ghostY - config.size / 2) + 'px',
                width: config.size + 'px', height: config.size + 'px',
                backgroundColor: activeColor, opacity: config.opacity,
                boxShadow: `0 0 ${config.rayIntensity * 0.8}px ${config.rayIntensity * 0.5}px ${activeColor}`,
                transform: `scale(${scale})`
            });

            while (modRow.firstChild) modRow.removeChild(modRow.firstChild);
            for (let i = 0; i < activeModifiers.length; i++) {
                let span = document.createElement('span');
                span.className = 'pill';
                span.appendChild(document.createTextNode(activeModifiers[i]));
                modRow.appendChild(span);
            }

            hud.style.left = ghostX + 'px';
            hud.style.top = (ghostY + config.size/2 + 16) + 'px';

            requestAnimationFrame(loop);
        }

        window._zenCreateRipple = createRipple;
        loop();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initUI);
    } else {
        initUI();
    }

    // Force listener execution in the capturing phase to override stopPropagation
    const captureOptions = { passive: true, capture: true };

    window.addEventListener('pointermove', e => { mouseX = e.clientX; mouseY = e.clientY; }, captureOptions);
    window.addEventListener('dragover', e => { mouseX = e.clientX; mouseY = e.clientY; }, captureOptions);

    window.addEventListener('pointerdown', e => {
        isClicking = true;
        if (window._zenCreateRipple) window._zenCreateRipple(e.clientX, e.clientY);
    }, captureOptions);

    window.addEventListener('pointerup', () => isClicking = false, captureOptions);
    window.addEventListener('pointercancel', () => isClicking = false, captureOptions);
    window.addEventListener('dragend', () => isClicking = false, captureOptions);

    window.addEventListener('wheel', () => {
        isScrolling = true; clearTimeout(scrollTimeout);
        scrollTimeout = setTimeout(() => isScrolling = false, 200);
    }, captureOptions);

    window.addEventListener('keydown', e => {
        const mods = [];
        if (e.ctrlKey) mods.push('Ctrl');
        if (e.shiftKey) mods.push('Shift');
        if (e.altKey) mods.push('Alt');
        if (e.metaKey) mods.push('Cmd');
        activeModifiers = mods;

        if (['Control', 'Shift', 'Alt', 'Meta', 'CapsLock', 'Tab', 'Escape', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'PageUp', 'PageDown', 'Home', 'End', 'Insert', 'Dead'].includes(e.key)) return;

        let keyName = e.key;
        if (keyName === ' ') keyName = '␣';
        else if (keyName === 'Enter') keyName = '↵';
        else if (keyName === 'Backspace' || keyName === 'Delete') {
            typedString = typedString.slice(0, -1);
            keyName = null;
        }

        if (keyName) {
            if (typedString.length > 25) typedString = typedString.substring(typedString.length - 24);
            typedString += keyName;
        }

        const textDisplay = document.querySelector('#zen-recording-container')?.shadowRoot?.querySelector('.typing-text');
        if (textDisplay) {
            if (typedString.length > 0) {
                while (textDisplay.firstChild) textDisplay.removeChild(textDisplay.firstChild);
                textDisplay.appendChild(document.createTextNode(typedString));
                textDisplay.style.opacity = '1';
            } else {
                textDisplay.style.opacity = '0';
            }

            clearTimeout(typeTimeout);
            typeTimeout = setTimeout(() => {
                textDisplay.style.opacity = '0';
                setTimeout(() => typedString = '', 200);
            }, 2000);
        }
    }, captureOptions);

    window.addEventListener('keyup', e => {
        const mods = [];
        if (e.ctrlKey) mods.push('Ctrl');
        if (e.shiftKey) mods.push('Shift');
        if (e.altKey) mods.push('Alt');
        if (e.metaKey) mods.push('Cmd');
        activeModifiers = mods;
    }, captureOptions);
})();
