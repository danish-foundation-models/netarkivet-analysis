/* shardstat.c -- single-pass aggregator over one netarkivet metadata CSV shard.
 *
 * Columns: hash,wayback_date,status_code,content_language,domain,url,
 *          content_type_full,content_text_length
 *
 * The source CSV is NOT valid RFC4180: ~1.9% of records are truncated
 * mid-record (they lose the trailing ,"content_type","length" and sometimes
 * part of the URL). A quote-state parser resynchronises by swallowing the
 * following record, which loses ~1.9% of rows and ~0.8% of domains.
 *
 * Instead we anchor on the record delimiter '\n"sha1:' and parse each record
 * from BOTH ends:
 *   - left  : hash,date,status,lang,domain are quote-free and comma-free,
 *             so five scans for '","' peel them exactly.
 *   - right : if the record ends with '"', peel content_text_length (digits)
 *             and content_type_full; a record truncated before those simply
 *             contributes no length/type but still contributes its domain.
 *
 * All accumulators are uint64_t and every addition is overflow-checked.
 *
 * Usage: shardstat <input.csv> <outdir> <tag>
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <inttypes.h>

/* ---------- string arena ---------- */
static char *arena = NULL;
static size_t arena_len = 0, arena_cap = 0;

static uint64_t arena_put(const char *s, size_t n) {
    if (arena_len + n + 1 > arena_cap) {
        arena_cap = arena_cap ? arena_cap * 2 : (1u << 20);
        while (arena_len + n + 1 > arena_cap) arena_cap *= 2;
        arena = realloc(arena, arena_cap);
        if (!arena) { fprintf(stderr, "arena OOM\n"); exit(1); }
    }
    uint64_t off = arena_len;
    memcpy(arena + off, s, n);
    arena[off + n] = '\0';
    arena_len += n + 1;
    return off;
}

/* ---------- hash map: string key -> counters ---------- */
#define NSLOT 3
typedef struct {
    uint64_t koff;      /* arena offset + 1; 0 == empty */
    uint64_t h;
    uint32_t klen;
    uint64_t v[NSLOT];
    uint64_t vmin, vmax;
} Ent;

typedef struct { Ent *e; uint64_t cap, used; } Map;

static uint64_t mixhash(const char *s, size_t n) {
    uint64_t h = 1469598103934665603ULL;
    for (size_t i = 0; i < n; i++) { h ^= (unsigned char)s[i]; h *= 1099511628211ULL; }
    h ^= h >> 33; h *= 0xff51afd7ed558ccdULL; h ^= h >> 33;
    h *= 0xc4ceb9fe1a85ec53ULL; h ^= h >> 33;
    return h;
}

static void map_init(Map *m, uint64_t cap) {
    m->cap = cap; m->used = 0;
    m->e = calloc(cap, sizeof(Ent));
    if (!m->e) { fprintf(stderr, "map OOM\n"); exit(1); }
}
static void map_grow(Map *m);

static Ent *map_get(Map *m, const char *k, size_t n) {
    uint64_t h = mixhash(k, n);
    uint64_t i = h & (m->cap - 1);
    for (;;) {
        Ent *e = &m->e[i];
        if (e->koff == 0) {
            e->koff = arena_put(k, n) + 1;
            e->klen = (uint32_t)n; e->h = h;
            e->vmin = UINT64_MAX; e->vmax = 0;
            m->used++;
            if (m->used * 10 >= m->cap * 7) { map_grow(m); return map_get(m, k, n); }
            return e;
        }
        if (e->h == h && e->klen == n && memcmp(arena + e->koff - 1, k, n) == 0) return e;
        i = (i + 1) & (m->cap - 1);
    }
}

static void map_grow(Map *m) {
    uint64_t oc = m->cap; Ent *oe = m->e;
    m->cap = oc * 2; m->used = 0;
    m->e = calloc(m->cap, sizeof(Ent));
    if (!m->e) { fprintf(stderr, "map grow OOM\n"); exit(1); }
    for (uint64_t j = 0; j < oc; j++) {
        if (!oe[j].koff) continue;
        uint64_t i = oe[j].h & (m->cap - 1);
        while (m->e[i].koff) i = (i + 1) & (m->cap - 1);
        m->e[i] = oe[j]; m->used++;
    }
    free(oe);
}

/* ---------- HyperLogLog (p=14) ---------- */
#define HLL_P 14
#define HLL_M (1u << HLL_P)
static uint8_t hll_hash[HLL_M], hll_url[HLL_M];

static void hll_add(uint8_t *reg, uint64_t h) {
    uint32_t idx = (uint32_t)(h >> (64 - HLL_P));
    uint64_t w = h << HLL_P;
    uint8_t rank = (uint8_t)(w == 0 ? (64 - HLL_P) + 1 : __builtin_clzll(w) + 1);
    if (rank > reg[idx]) reg[idx] = rank;
}

/* ---------- overflow-checked add ---------- */
static uint64_t overflow_events = 0;
static inline uint64_t addck(uint64_t a, uint64_t b) {
    uint64_t r;
    if (__builtin_add_overflow(a, b, &r)) { overflow_events++; return UINT64_MAX; }
    return r;
}

static uint64_t parse_u64(const char *s, size_t n, int *ok) {
    uint64_t v = 0;
    if (n == 0 || n > 20) { *ok = 0; return 0; }
    for (size_t i = 0; i < n; i++) {
        if (s[i] < '0' || s[i] > '9') { *ok = 0; return 0; }
        if (__builtin_mul_overflow(v, 10ULL, &v) ||
            __builtin_add_overflow(v, (uint64_t)(s[i] - '0'), &v)) { *ok = 0; return 0; }
    }
    *ok = 1; return v;
}

/* ---------- globals ---------- */
static Map m_dom, m_lang, m_mime, m_status, m_ym;
static uint64_t n_rec = 0, n_leftfail = 0, n_complete = 0, n_trunc_len = 0,
                n_trunc_url = 0, n_nodomain = 0, n_baddate = 0, n_badstatus = 0;
static uint64_t total_textlen = 0;
static uint64_t lenhist[48];
static char lowbuf[512];

static const char *lower_dup(const char *s, size_t n) {
    if (n > sizeof lowbuf - 1) n = sizeof lowbuf - 1;
    for (size_t i = 0; i < n; i++) {
        char c = s[i];
        lowbuf[i] = (c >= 'A' && c <= 'Z') ? c + 32 : c;
    }
    return lowbuf;
}

/* find next occurrence of '","' in [p, end) */
static const char *find_sep(const char *p, const char *end) {
    while (p + 2 < end) {
        const char *q = memchr(p, '"', (size_t)(end - p) - 2);
        if (!q) return NULL;
        if (q[1] == ',' && q[2] == '"') return q;
        p = q + 1;
    }
    return NULL;
}

static void emit_record(const char *r, size_t n) {
    n_rec++;
    while (n && (r[n-1] == '\n' || r[n-1] == '\r')) n--;
    if (n < 12 || r[0] != '"') { n_leftfail++; return; }

    const char *end = r + n;
    const char *fs[6];          /* fs[i] = start of field i */
    const char *sep;
    size_t flen[6];
    const char *p = r + 1;
    fs[0] = p;
    int i;
    for (i = 0; i < 5; i++) {
        sep = find_sep(p, end);
        if (!sep) break;
        flen[i] = (size_t)(sep - fs[i]);
        fs[i+1] = sep + 3;
        p = sep + 3;
    }
    if (i < 5) { n_leftfail++; return; }
    const char *url_start = fs[5];

    /* ---- right peel ---- */
    const char *ct = NULL; size_t ctn = 0;
    uint64_t tlen = 0; int have_len = 0;
    const char *url_end = end;

    if (end > url_start && end[-1] == '"') {
        /* last field = [j+3, end-1) where j is the last '","' after url_start */
        const char *j = NULL;
        for (const char *q = end - 3; q >= url_start; q--)
            if (q[0] == '"' && q[1] == ',' && q[2] == '"') { j = q; break; }
        if (j) {
            const char *lastf = j + 3; size_t lastn = (size_t)(end - 1 - lastf);
            int ok = 0;
            uint64_t v = parse_u64(lastf, lastn, &ok);
            if (ok) {
                tlen = v; have_len = 1;
                /* one more peel to the left for content_type */
                const char *k = NULL;
                for (const char *q = j - 1; q >= url_start; q--)
                    if (q[0] == '"' && q[1] == ',' && q[2] == '"') { k = q; break; }
                if (k) { ct = k + 3; ctn = (size_t)(j - ct); url_end = k + 1; }
                else   { url_end = j + 1; }
            } else if (memchr(lastf, '/', lastn)) {
                ct = lastf; ctn = lastn; url_end = j + 1;   /* truncated after ctype */
            }
        }
    }

    if (have_len && ct)      n_complete++;
    else if (ct)             n_trunc_len++;
    else                     n_trunc_url++;

    total_textlen = addck(total_textlen, tlen);
    {
        int b = tlen == 0 ? 0 : 64 - __builtin_clzll(tlen);
        if (b > 47) b = 47;
        lenhist[b]++;
    }

    /* ---- status ---- */
    int sok = 0;
    uint64_t st = parse_u64(fs[2], flen[2], &sok);
    if (!sok) n_badstatus++;
    int is200 = (sok && st == 200);

    /* ---- date ---- */
    uint64_t date8 = 0; int dok = 0;
    if (flen[1] >= 8) date8 = parse_u64(fs[1], 8, &dok);
    if (!dok || date8 < 19900101ULL || date8 > 20401231ULL) { n_baddate++; date8 = 0; }

    /* ---- domain ---- */
    size_t dn = flen[4];
    if (dn == 0) n_nodomain++;
    else {
        if (dn > 253) dn = 253;
        Ent *e = map_get(&m_dom, lower_dup(fs[4], dn), dn);
        e->v[0]++;
        e->v[1] = addck(e->v[1], tlen);
        e->v[2] += is200 ? 1 : 0;
        if (date8) {
            if (date8 < e->vmin) e->vmin = date8;
            if (date8 > e->vmax) e->vmax = date8;
        }
    }

    /* ---- language ---- */
    { size_t ln = flen[3] > 16 ? 16 : flen[3];
      if (ln) map_get(&m_lang, lower_dup(fs[3], ln), ln)->v[0]++;
      else    map_get(&m_lang, "(none)", 6)->v[0]++; }

    /* ---- status table ---- */
    { size_t sn = flen[2] > 8 ? 8 : flen[2];
      if (sn) map_get(&m_status, fs[2], sn)->v[0]++;
      else    map_get(&m_status, "(none)", 6)->v[0]++; }

    /* ---- mime (strip ';' parameters) ---- */
    if (ct) {
        size_t mn = ctn;
        for (size_t k = 0; k < mn; k++) if (ct[k] == ';') { mn = k; break; }
        while (mn && (ct[mn-1] == ' ' || ct[mn-1] == '\t')) mn--;
        if (mn > 100) mn = 100;
        if (mn) map_get(&m_mime, lower_dup(ct, mn), mn)->v[0]++;
        else    map_get(&m_mime, "(empty)", 7)->v[0]++;
    } else {
        map_get(&m_mime, "(truncated)", 11)->v[0]++;
    }

    /* ---- year-month ---- */
    if (date8) {
        uint64_t y = date8 / 10000, mo = (date8 / 100) % 100;
        if (mo >= 1 && mo <= 12) {
            char ym[7];
            ym[0]='0'+(y/1000)%10; ym[1]='0'+(y/100)%10; ym[2]='0'+(y/10)%10; ym[3]='0'+y%10;
            ym[4]='-'; ym[5]='0'+mo/10; ym[6]='0'+mo%10;
            map_get(&m_ym, ym, 7)->v[0]++;
        }
    }

    /* ---- sketches ---- */
    hll_add(hll_hash, mixhash(fs[0], flen[0]));
    if (url_end > url_start) hll_add(hll_url, mixhash(url_start, (size_t)(url_end - url_start)));
}

#define DELIM "\n\"sha1:"
#define DELIM_LEN 7

int main(int argc, char **argv) {
    if (argc != 4) { fprintf(stderr, "usage: %s <in.csv> <outdir> <tag>\n", argv[0]); return 2; }
    FILE *in = fopen(argv[1], "rb");
    if (!in) { perror(argv[1]); return 1; }

    map_init(&m_dom, 1u << 21);
    map_init(&m_lang, 1u << 10);
    map_init(&m_mime, 1u << 14);
    map_init(&m_status, 1u << 10);
    map_init(&m_ym, 1u << 12);

    const size_t CAP = 64u << 20;
    char *buf = malloc(CAP);
    if (!buf) { fprintf(stderr, "buf OOM\n"); return 1; }
    size_t have = 0;
    int first_chunk = 1;
    uint64_t oversize = 0;

    for (;;) {
        size_t got = fread(buf + have, 1, CAP - have, in);
        have += got;
        int eof = (got == 0);

        char *start = buf;
        if (first_chunk) {                      /* drop the header line */
            char *nl = memchr(buf, '\n', have);
            if (nl) { start = nl + 1; first_chunk = 0; }
            else if (eof) break;
            else { /* header longer than buffer: impossible, bail */ break; }
        }

        char *cur = start;
        char *limit = buf + have;
        for (;;) {
            char *d = memmem(cur, (size_t)(limit - cur), DELIM, DELIM_LEN);
            if (!d) break;
            emit_record(cur, (size_t)(d - cur));
            cur = d + 1;                        /* keep the leading '"' of the record */
        }

        if (eof) { if (limit > cur) emit_record(cur, (size_t)(limit - cur)); break; }

        size_t rem = (size_t)(limit - cur);
        if (rem == have && have == CAP) {       /* no delimiter in a full buffer */
            emit_record(cur, rem); oversize++; have = 0;
        } else {
            memmove(buf, cur, rem);
            have = rem;
        }
    }
    fclose(in);

    char path[4096];
    snprintf(path, sizeof path, "%s/dom.%s.tsv", argv[2], argv[3]);
    FILE *o = fopen(path, "w");
    if (!o) { perror(path); return 1; }
    for (uint64_t i = 0; i < m_dom.cap; i++) {
        Ent *e = &m_dom.e[i];
        if (!e->koff) continue;
        fprintf(o, "%s\t%" PRIu64 "\t%" PRIu64 "\t%" PRIu64 "\t%" PRIu64 "\t%" PRIu64 "\n",
                arena + e->koff - 1, e->v[0], e->v[1], e->v[2],
                e->vmin == UINT64_MAX ? 0 : e->vmin, e->vmax);
    }
    if (fclose(o)) { perror("write dom"); return 1; }

    snprintf(path, sizeof path, "%s/misc.%s.tsv", argv[2], argv[3]);
    o = fopen(path, "w");
    if (!o) { perror(path); return 1; }
    fprintf(o, "STAT\trecords\t%" PRIu64 "\n", n_rec);
    fprintf(o, "STAT\tcomplete\t%" PRIu64 "\n", n_complete);
    fprintf(o, "STAT\ttrunc_len\t%" PRIu64 "\n", n_trunc_len);
    fprintf(o, "STAT\ttrunc_url\t%" PRIu64 "\n", n_trunc_url);
    fprintf(o, "STAT\tleftfail\t%" PRIu64 "\n", n_leftfail);
    fprintf(o, "STAT\tnodomain\t%" PRIu64 "\n", n_nodomain);
    fprintf(o, "STAT\tbaddate\t%" PRIu64 "\n", n_baddate);
    fprintf(o, "STAT\tbadstatus\t%" PRIu64 "\n", n_badstatus);
    fprintf(o, "STAT\ttotal_textlen\t%" PRIu64 "\n", total_textlen);
    fprintf(o, "STAT\toverflow_events\t%" PRIu64 "\n", overflow_events);
    fprintf(o, "STAT\toversize\t%" PRIu64 "\n", oversize);
    fprintf(o, "STAT\tuniq_domains\t%" PRIu64 "\n", m_dom.used);
    for (int b = 0; b < 48; b++)
        if (lenhist[b]) fprintf(o, "LENHIST\t%d\t%" PRIu64 "\n", b, lenhist[b]);
    struct { const char *tag; Map *m; } tabs[] = {
        {"LANG",&m_lang},{"MIME",&m_mime},{"STATUS",&m_status},{"YM",&m_ym} };
    for (int t = 0; t < 4; t++)
        for (uint64_t i = 0; i < tabs[t].m->cap; i++) {
            Ent *e = &tabs[t].m->e[i];
            if (!e->koff) continue;
            fprintf(o, "%s\t%s\t%" PRIu64 "\n", tabs[t].tag, arena + e->koff - 1, e->v[0]);
        }
    for (uint32_t i = 0; i < HLL_M; i++) if (hll_hash[i]) fprintf(o, "HLLH\t%u\t%u\n", i, hll_hash[i]);
    for (uint32_t i = 0; i < HLL_M; i++) if (hll_url[i]) fprintf(o, "HLLU\t%u\t%u\n", i, hll_url[i]);
    if (fclose(o)) { perror("write misc"); return 1; }
    return 0;
}
