// SPDX-License-Identifier: Apache-2.0
//
// Character-grid renderer for extCLI, loaded at runtime from
// extcli/dex/terminal.dex by extcli/src/term/bridge.py.
//
// Python drives what to show; this owns how it is drawn. Two reasons it is not
// Python: the TUI mode repaints tens of thousands of glyphs per frame, and a
// terminal has to re-wrap its whole scrollback on rotation. Both are hot loops
// where a per-call JNI hop would be the cost.
//
// Two content modes:
//   stream -- append(): text with ANSI/SGR escapes, wrapped into the grid
//   grid   -- blit():   a complete frame of cells, used by the TUI overlay
//
// Compiles against android-all only: no host classes, no Xposed, no reflection.

package dev.vcvkk.extcli.terminal

import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.Typeface
import android.util.TypedValue
import android.view.View
import android.view.ViewGroup
import android.widget.FrameLayout
import android.widget.ScrollView

object TerminalNative {

    // palette layout shared with render/palette.py: nine semantic roles,
    // then the sixteen ANSI colors
    const val ROLE_BG = 0
    const val ROLE_FG = 1
    const val ROLE_DIM = 2
    const val ROLE_ACCENT = 3
    const val ROLE_ERROR = 4
    const val ROLE_SUCCESS = 5
    const val ROLE_WARN = 6
    const val ROLE_SELECTION = 7
    const val ROLE_DIVIDER = 8
    const val ROLE_COUNT = 9
    const val PALETTE_SIZE = ROLE_COUNT + 16

    const val VERSION = 2

    private val sessions = HashMap<View, TerminalView>()

    @JvmStatic
    fun version(): Int = VERSION

    /**
     * Builds the terminal and returns the scrollable root to hand to Android.
     * Keep the returned View; every other call here takes it back.
     */
    @JvmStatic
    fun create(
        ctx: Context,
        textSizeSp: Float,
        typeface: Typeface?,
        palette: IntArray,
        scrollbackLines: Int
    ): View {
        val term = TerminalView(ctx, textSizeSp, typeface, normalizePalette(palette), scrollbackLines)
        val scroll = ScrollView(ctx)
        scroll.isFillViewport = true
        scroll.setBackgroundColor(term.palette[ROLE_BG])
        scroll.addView(
            term,
            FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
            )
        )
        sessions[scroll] = term
        return scroll
    }

    @JvmStatic
    fun release(root: View) {
        sessions.remove(root)
    }

    /** Appends text in stream mode. Understands \n, \r, \t, \b and SGR colors. */
    @JvmStatic
    fun append(root: View, text: String) {
        val term = sessions[root] ?: return
        term.append(text)
    }

    /** Replaces everything with one complete frame of cells (TUI mode). */
    @JvmStatic
    fun blit(root: View, chars: IntArray, fg: IntArray, bg: IntArray, cols: Int, rows: Int) {
        val term = sessions[root] ?: return
        term.blit(chars, fg, bg, cols, rows)
    }

    @JvmStatic
    fun clear(root: View) {
        sessions[root]?.clearAll()
    }

    @JvmStatic
    fun setPalette(root: View, palette: IntArray) {
        val term = sessions[root] ?: return
        term.applyPalette(normalizePalette(palette))
        (root as? ScrollView)?.setBackgroundColor(term.palette[ROLE_BG])
    }

    @JvmStatic
    fun setTextSize(root: View, textSizeSp: Float) {
        sessions[root]?.setTextSizeSp(textSizeSp)
    }

    /** [cols, rows, cellWidthPx, cellHeightPx] — what Python needs to lay out. */
    @JvmStatic
    fun metrics(root: View): IntArray {
        val term = sessions[root] ?: return intArrayOf(0, 0, 0, 0)
        return intArrayOf(term.cols, term.visualRowCount(), term.cellWidth.toInt(), term.cellHeight.toInt())
    }

    /** Human-readable state, for diagnostics when the screen looks empty. */
    @JvmStatic
    fun describe(root: View): String {
        val term = sessions[root] ?: return "no session for this view"
        return term.describe()
    }

    @JvmStatic
    fun scrollToBottom(root: View) {
        val scroll = root as? ScrollView ?: return
        scroll.post { scroll.fullScroll(View.FOCUS_DOWN) }
    }

    /** Plain text of the whole scrollback, for copy-to-clipboard. */
    @JvmStatic
    fun getText(root: View): String = sessions[root]?.plainText() ?: ""

    private fun normalizePalette(palette: IntArray?): IntArray {
        val out = IntArray(PALETTE_SIZE)
        // sane defaults so a short array from Python cannot make text invisible
        out[ROLE_BG] = 0xFF101010.toInt()
        out[ROLE_FG] = 0xFFE6E6E6.toInt()
        out[ROLE_DIM] = 0xFF8A8A8A.toInt()
        out[ROLE_ACCENT] = 0xFF4EA1F3.toInt()
        out[ROLE_ERROR] = 0xFFE0574B.toInt()
        out[ROLE_SUCCESS] = 0xFF5FB85F.toInt()
        out[ROLE_WARN] = 0xFFE0A03C.toInt()
        out[ROLE_SELECTION] = 0xFF2A3A4A.toInt()
        out[ROLE_DIVIDER] = 0xFF303030.toInt()
        for (i in 0 until 16) out[ROLE_COUNT + i] = DEFAULT_ANSI[i]
        if (palette != null) {
            for (i in 0 until minOf(palette.size, PALETTE_SIZE)) out[i] = palette[i]
        }
        return out
    }

    private val DEFAULT_ANSI = intArrayOf(
        0xFF3B3B3B.toInt(), 0xFFE0574B.toInt(), 0xFF5FB85F.toInt(), 0xFFE0A03C.toInt(),
        0xFF4EA1F3.toInt(), 0xFFB07BD8.toInt(), 0xFF4EC3C3.toInt(), 0xFFCFCFCF.toInt(),
        0xFF6B6B6B.toInt(), 0xFFF07568.toInt(), 0xFF7CD07C.toInt(), 0xFFF0BC5E.toInt(),
        0xFF6FB6F7.toInt(), 0xFFC79BE6.toInt(), 0xFF6FD6D6.toInt(), 0xFFFFFFFF.toInt()
    )
}

private const val ESC = '\u001B'

/**
 * One styled run of text inside a logical line. Runs, not characters, are the
 * unit of drawing: real output has long stretches of one color, so this keeps
 * drawText calls proportional to color changes instead of glyphs.
 */
private class Span(val text: String, val fg: Int, val bg: Int, val bold: Boolean)

private class LogicalLine {
    val spans = ArrayList<Span>(4)
    var length = 0

    fun add(span: Span) {
        if (span.text.isEmpty()) return
        spans.add(span)
        length += span.text.length
    }

    fun plain(): String {
        val sb = StringBuilder(length)
        for (s in spans) sb.append(s.text)
        return sb.toString()
    }
}

/** A wrapped, drawable row: slice of a logical line clipped to the grid width. */
private class VisualRow(val line: LogicalLine, val start: Int, val end: Int)

private class TerminalView(
    ctx: Context,
    textSizeSp: Float,
    typeface: Typeface?,
    var palette: IntArray,
    private val scrollbackLimit: Int
) : View(ctx) {

    private val paint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val bgPaint = Paint()
    private val boldTypeface: Typeface?
    private val plainTypeface: Typeface?

    var cellWidth = 0f
        private set
    var cellHeight = 0f
        private set
    private var baseline = 0f
    var cols = 1
        private set

    private val lines = ArrayList<LogicalLine>(256)
    private var visualRows = ArrayList<VisualRow>(256)
    private var wrapValidForCols = -1

    // stream-mode SGR state
    private var curFg = -1
    private var curBg = -1
    private var curBold = false

    // grid mode (TUI): a full frame, drawn as-is
    private var gridChars: IntArray? = null
    private var gridFg: IntArray? = null
    private var gridBg: IntArray? = null
    private var gridCols = 0
    private var gridRows = 0

    init {
        plainTypeface = typeface ?: Typeface.MONOSPACE
        boldTypeface = Typeface.create(plainTypeface, Typeface.BOLD)
        paint.typeface = plainTypeface
        paint.textSize = spToPx(textSizeSp)
        measureCell()
        lines.add(LogicalLine())
        setBackgroundColor(palette[TerminalNative.ROLE_BG])
    }

    private fun spToPx(sp: Float): Float =
        TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_SP, sp, resources.displayMetrics)

    private fun measureCell() {
        // the font is monospace, so one glyph's advance is the cell width
        cellWidth = paint.measureText("M")
        if (cellWidth <= 0f) cellWidth = paint.textSize * 0.6f
        val fm = paint.fontMetrics
        cellHeight = (fm.descent - fm.ascent) * 1.05f
        baseline = -fm.ascent
    }

    fun setTextSizeSp(sp: Float) {
        paint.textSize = spToPx(sp)
        measureCell()
        wrapValidForCols = -1
        requestLayout()
        invalidate()
    }

    fun applyPalette(newPalette: IntArray) {
        palette = newPalette
        setBackgroundColor(palette[TerminalNative.ROLE_BG])
        invalidate()
    }

    // ------------------------------------------------------------ stream mode

    fun append(text: String) {
        if (gridChars != null) {
            // leaving TUI mode: drop the frame, go back to the scrollback
            gridChars = null
            gridFg = null
            gridBg = null
        }
        var i = 0
        val pending = StringBuilder()
        while (i < text.length) {
            val ch = text[i]
            when {
                ch == ESC && i + 1 < text.length && text[i + 1] == '[' -> {
                    flush(pending)
                    i = handleCsi(text, i + 2)
                }
                ch == '\n' -> {
                    flush(pending)
                    newLine()
                    i++
                }
                ch == '\r' -> {
                    // carriage return without newline rewrites the current line
                    flush(pending)
                    currentLine().let { line ->
                        line.spans.clear()
                        line.length = 0
                    }
                    invalidateWrap()
                    i++
                }
                ch == '\t' -> {
                    val width = 8 - (currentLine().length + pending.length) % 8
                    for (k in 0 until width) pending.append(' ')
                    i++
                }
                ch == '\b' -> {
                    if (pending.isNotEmpty()) pending.setLength(pending.length - 1)
                    i++
                }
                ch.code < 32 -> i++  // other control characters are not ours to render
                else -> {
                    pending.append(ch)
                    i++
                }
            }
        }
        flush(pending)
        trimScrollback()
        invalidateWrap()
        requestLayout()
        invalidate()
    }

    private fun currentLine(): LogicalLine {
        if (lines.isEmpty()) lines.add(LogicalLine())
        return lines[lines.size - 1]
    }

    private fun flush(pending: StringBuilder) {
        if (pending.isEmpty()) return
        currentLine().add(Span(pending.toString(), curFg, curBg, curBold))
        pending.setLength(0)
    }

    private fun newLine() {
        lines.add(LogicalLine())
    }

    private fun trimScrollback() {
        if (scrollbackLimit <= 0) return
        while (lines.size > scrollbackLimit) lines.removeAt(0)
    }

    /** Parses one CSI sequence, returns the index just past it. */
    private fun handleCsi(text: String, start: Int): Int {
        var i = start
        val params = StringBuilder()
        while (i < text.length) {
            val ch = text[i]
            if (ch in '0'..'9' || ch == ';') {
                params.append(ch)
                i++
                continue
            }
            when (ch) {
                'm' -> applySgr(params.toString())
                'J', 'K' -> {
                    // clear screen / clear line: the closest thing that makes
                    // sense in a scrollback is dropping the current line
                    currentLine().spans.clear()
                    currentLine().length = 0
                    if (ch == 'J') {
                        lines.clear()
                        lines.add(LogicalLine())
                    }
                    invalidateWrap()
                }
                // cursor movement has no meaning in an append-only scrollback
                else -> Unit
            }
            return i + 1
        }
        return i
    }

    private fun applySgr(paramText: String) {
        if (paramText.isEmpty()) {
            resetSgr()
            return
        }
        val parts = paramText.split(';')
        var i = 0
        while (i < parts.size) {
            val code = parts[i].toIntOrNull() ?: 0
            when {
                code == 0 -> resetSgr()
                code == 1 -> curBold = true
                code == 22 -> curBold = false
                code == 39 -> curFg = -1
                code == 49 -> curBg = -1
                code in 30..37 -> curFg = ansi(code - 30)
                code in 90..97 -> curFg = ansi(code - 90 + 8)
                code in 40..47 -> curBg = ansi(code - 40)
                code in 100..107 -> curBg = ansi(code - 100 + 8)
                code == 38 || code == 48 -> {
                    // 38;2;r;g;b truecolor, or 38;5;n from the 256-color cube
                    val mode = parts.getOrNull(i + 1)?.toIntOrNull()
                    if (mode == 2 && i + 4 < parts.size) {
                        val color = rgb(
                            parts[i + 2].toIntOrNull() ?: 0,
                            parts[i + 3].toIntOrNull() ?: 0,
                            parts[i + 4].toIntOrNull() ?: 0
                        )
                        if (code == 38) curFg = color else curBg = color
                        i += 4
                    } else if (mode == 5 && i + 2 < parts.size) {
                        val color = xterm256(parts[i + 2].toIntOrNull() ?: 0)
                        if (code == 38) curFg = color else curBg = color
                        i += 2
                    }
                }
            }
            i++
        }
    }

    private fun resetSgr() {
        curFg = -1
        curBg = -1
        curBold = false
    }

    private fun ansi(index: Int): Int =
        palette[TerminalNative.ROLE_COUNT + (index and 15)]

    private fun rgb(r: Int, g: Int, b: Int): Int =
        (0xFF shl 24) or ((r and 255) shl 16) or ((g and 255) shl 8) or (b and 255)

    private fun xterm256(n: Int): Int = when {
        n < 16 -> ansi(n)
        n < 232 -> {
            val i = n - 16
            val steps = intArrayOf(0, 95, 135, 175, 215, 255)
            rgb(steps[i / 36], steps[(i / 6) % 6], steps[i % 6])
        }
        else -> {
            val level = 8 + (n - 232) * 10
            rgb(level, level, level)
        }
    }

    // -------------------------------------------------------------- grid mode

    fun blit(chars: IntArray, fg: IntArray, bg: IntArray, cols: Int, rows: Int) {
        if (cols <= 0 || rows <= 0) return
        val needed = cols * rows
        if (chars.size < needed || fg.size < needed || bg.size < needed) return
        gridChars = chars
        gridFg = fg
        gridBg = bg
        gridCols = cols
        gridRows = rows
        requestLayout()
        invalidate()
    }

    fun clearAll() {
        lines.clear()
        lines.add(LogicalLine())
        gridChars = null
        gridFg = null
        gridBg = null
        resetSgr()
        invalidateWrap()
        requestLayout()
        invalidate()
    }

    // ------------------------------------------------------------ wrap/layout

    private fun invalidateWrap() {
        wrapValidForCols = -1
    }

    private fun rewrap() {
        val rows = ArrayList<VisualRow>(lines.size + 8)
        val width = maxOf(cols, 1)
        for (line in lines) {
            if (line.length == 0) {
                rows.add(VisualRow(line, 0, 0))
                continue
            }
            var start = 0
            while (start < line.length) {
                val end = minOf(start + width, line.length)
                rows.add(VisualRow(line, start, end))
                start = end
            }
        }
        visualRows = rows
        wrapValidForCols = cols
    }

    fun visualRowCount(): Int {
        gridChars?.let { return gridRows }
        if (wrapValidForCols != cols) rewrap()
        return visualRows.size
    }

    override fun onMeasure(widthMeasureSpec: Int, heightMeasureSpec: Int) {
        val width = MeasureSpec.getSize(widthMeasureSpec)
        val newCols = if (cellWidth > 0f) maxOf((width / cellWidth).toInt(), 1) else 1
        if (newCols != cols) {
            cols = newCols
            invalidateWrap()
        }
        val rows = visualRowCount()
        // never measure to nothing: a zero-height terminal is indistinguishable
        // from a broken one, and one empty row still shows the caret area
        val minimum = maxOf(cellHeight.toInt(), 1)
        val contentHeight = maxOf(
            maxOf((rows * cellHeight).toInt(), minimum),
            MeasureSpec.getSize(heightMeasureSpec)
        )
        setMeasuredDimension(maxOf(width, 1), contentHeight)
    }

    // ------------------------------------------------------------------- draw

    override fun onDraw(canvas: Canvas) {
        canvas.drawColor(palette[TerminalNative.ROLE_BG])
        if (gridChars != null) {
            drawGrid(canvas)
        } else {
            drawScrollback(canvas)
        }
    }

    private fun drawScrollback(canvas: Canvas) {
        if (wrapValidForCols != cols) rewrap()
        // only the rows inside the clip are worth touching: the scrollback can
        // be thousands of lines while the window shows a few dozen
        val clip = canvas.clipBounds
        val first = maxOf((clip.top / cellHeight).toInt() - 1, 0)
        val last = minOf((clip.bottom / cellHeight).toInt() + 1, visualRows.size - 1)
        val defaultFg = palette[TerminalNative.ROLE_FG]

        for (rowIndex in first..last) {
            val row = visualRows.getOrNull(rowIndex) ?: continue
            val y = rowIndex * cellHeight
            var column = 0
            var consumed = 0
            for (span in row.line.spans) {
                val spanStart = consumed
                val spanEnd = consumed + span.text.length
                consumed = spanEnd
                if (spanEnd <= row.start) continue
                if (spanStart >= row.end) break
                val from = maxOf(row.start, spanStart) - spanStart
                val to = minOf(row.end, spanEnd) - spanStart
                if (to <= from) continue
                val piece = span.text.substring(from, to)
                val x = column * cellWidth
                if (span.bg != -1) {
                    bgPaint.color = span.bg
                    canvas.drawRect(x, y, x + piece.length * cellWidth, y + cellHeight, bgPaint)
                }
                paint.color = if (span.fg != -1) span.fg else defaultFg
                paint.typeface = if (span.bold) boldTypeface else plainTypeface
                canvas.drawText(piece, x, y + baseline, paint)
                column += piece.length
            }
        }
    }

    private fun drawGrid(canvas: Canvas) {
        val chars = gridChars ?: return
        val fg = gridFg ?: return
        val bg = gridBg ?: return
        val clip = canvas.clipBounds
        val first = maxOf((clip.top / cellHeight).toInt() - 1, 0)
        val last = minOf((clip.bottom / cellHeight).toInt() + 1, gridRows - 1)
        val buffer = CharArray(gridCols)

        for (row in first..last) {
            val y = row * cellHeight
            val base = row * gridCols
            // background first, merged into runs of equal color
            var col = 0
            while (col < gridCols) {
                val color = bg[base + col]
                var span = 1
                while (col + span < gridCols && bg[base + col + span] == color) span++
                if (color != 0) {
                    bgPaint.color = color
                    canvas.drawRect(
                        col * cellWidth, y, (col + span) * cellWidth, y + cellHeight, bgPaint
                    )
                }
                col += span
            }
            // then glyphs, also merged by color
            paint.typeface = plainTypeface
            col = 0
            while (col < gridCols) {
                val color = fg[base + col]
                var span = 0
                while (col + span < gridCols && fg[base + col + span] == color) {
                    buffer[span] = chars[base + col + span].toChar()
                    span++
                }
                paint.color = color
                canvas.drawText(buffer, 0, span, col * cellWidth, y + baseline, paint)
                col += span
            }
        }
    }

    fun describe(): String {
        val mode = if (gridChars != null) "grid" else "stream"
        return "mode=%s cols=%d rows=%d cell=%.1fx%.1f lines=%d size=%dx%d".format(
            mode, cols, visualRowCount(), cellWidth, cellHeight, lines.size,
            width, height
        )
    }

    fun plainText(): String {
        gridChars?.let { chars ->
            val sb = StringBuilder(gridRows * (gridCols + 1))
            for (row in 0 until gridRows) {
                for (col in 0 until gridCols) sb.append(chars[row * gridCols + col].toChar())
                sb.append('\n')
            }
            return sb.toString()
        }
        val sb = StringBuilder()
        for (line in lines) {
            sb.append(line.plain())
            sb.append('\n')
        }
        return sb.toString()
    }
}
