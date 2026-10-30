// Copyright (C) 2026 Tim Michals
// SPDX-License-Identifier: GPL-3.0-or-later
//
// libpio2sim: C ABI around the Verilator model of pio2_core (unmodified tile + trace tap + event FIFO).
// @impl [SPEC-DBG-01] [SPEC-DBG-13] docs/DEBUGGER_SPECIFICATION.md

#include "Vpio2_core.h"
#include "verilated.h"
#include "verilated_save.h"
#include "verilated_vpi.h"
#include "vpi_user.h"

#include <cstdint>
#include <cstring>
#include <string>
#include <unordered_map>
#include <vector>

namespace {

constexpr uint32_t kBase = 0x00000000u;   // the tile's window in pio2_core

struct Sim {
    VerilatedContext ctx;
    Vpio2_core* top;
    std::vector<uint64_t> events;   // trace records taken from the core's record stream
    uint64_t cycle = 0;
    uint32_t ext = 0;        // levels the parts drive
    uint32_t ext_mask = 0;   // lines the parts drive
    uint32_t pull = 0xFFFFFFFFu;   // level of a line nobody drives
    uint32_t oe_inv = 0;     // lines whose output enable the IO block inverts (RP2040 GPIO OEOVER)
    std::unordered_map<std::string, vpiHandle> handles;
    vpiHandle sm_pc[4] = {};
    vpiHandle sm_stall[4] = {};

    // Every clock is recorded, including those spent in register accesses ([SPEC-DBG-01] pads):
    // the trace has no gaps. Python drains this queue after each call that clocks the model.
    struct Rec { uint32_t out, dir, in; uint8_t pc[4], stall[4]; };
    std::vector<Rec> pending;
    bool record = false;

    Sim() { top = new Vpio2_core(&ctx, "TOP"); }
    ~Sim() { top->final(); delete top; }

    uint32_t pad_dir() const { return top->o_gpio_pindir ^ oe_inv; }

    uint32_t pads() const {
        const uint32_t out = top->o_gpio_out, dir = pad_dir();
        const uint32_t outside = (ext & ext_mask) | (pull & ~ext_mask);
        return (out & dir) | (outside & ~dir);
    }

    // One system clock: inputs settle, rising edge, falling edge.
    void step() {
        top->i_gpio = pads();
        top->eval();
        if (top->m_trace_tvalid && top->m_trace_tready) events.push_back(top->m_trace_tdata);
        top->clk = 1;
        top->eval();
        ctx.timeInc(5);
        top->clk = 0;
        top->eval();
        ctx.timeInc(5);
        ++cycle;
        top->i_gpio = pads();
        top->eval();
        if (record) {
            Rec r{top->o_gpio_out, pad_dir(), pads(), {}, {}};
            for (int m = 0; m < 4; ++m) {
                r.pc[m] = static_cast<uint8_t>(read32_fn(sm_pc[m]));
                r.stall[m] = static_cast<uint8_t>(read32_fn(sm_stall[m]));
            }
            pending.push_back(r);
        }
    }
    static uint32_t read32_fn(vpiHandle h);
};

// Verilator save / restore into memory instead of a file ([SPEC-DBG-06] snapshots).
class MemSave final : public VerilatedSerialize {
public:
    std::vector<uint8_t> data;
    MemSave() { m_isOpen = true; m_cp = m_bufp; header(); }
    void finish() { trailer(); flush(); m_isOpen = false; }
    void flush() override {
        data.insert(data.end(), m_bufp, m_cp);
        m_cp = m_bufp;
    }
};

class MemRestore final : public VerilatedDeserialize {
    const uint8_t* m_src;
    size_t m_left;
public:
    MemRestore(const uint8_t* src, size_t n) : m_src(src), m_left(n) {
        m_isOpen = true;
        m_cp = m_endp = m_bufp;
        header();
    }
    void finish() { trailer(); m_isOpen = false; }
    void fill() override {
        uint8_t* rp = m_bufp;
        for (const uint8_t* sp = m_cp; sp < m_endp; *rp++ = *sp++) {}
        m_endp = m_bufp + (m_endp - m_cp);
        m_cp = m_bufp;
        const size_t room = static_cast<size_t>(m_bufp + bufferSize() - m_endp);
        const size_t n = room < m_left ? room : m_left;
        std::memcpy(m_endp, m_src, n);
        m_endp += n;
        m_src += n;
        m_left -= n;
    }
};

Sim* g_sim = nullptr;   // VPI names one design per process

vpiHandle lookup(Sim* s, const char* name) {
    auto it = s->handles.find(name);
    if (it != s->handles.end()) return it->second;
    std::string full = std::string("TOP.pio2_core.u_tile.") + name;
    vpiHandle h = vpi_handle_by_name(const_cast<PLI_BYTE8*>(full.c_str()), nullptr);
    s->handles.emplace(name, h);
    return h;
}

int read_handle(vpiHandle h, uint32_t* words, int nwords) {
    if (!h) return -1;
    s_vpi_value v;
    v.format = vpiVectorVal;
    vpi_get_value(h, &v);
    const int width = vpi_get(vpiSize, h);
    const int n = (width + 31) / 32;
    for (int i = 0; i < nwords; ++i) words[i] = i < n ? static_cast<uint32_t>(v.value.vector[i].aval) : 0u;
    return width;
}

uint32_t read32(vpiHandle h) {
    uint32_t w = 0;
    read_handle(h, &w, 1);
    return w;
}

uint32_t Sim::read32_fn(vpiHandle h) { return read32(h); }

}  // namespace

extern "C" {

void* pio2sim_create() {
    if (g_sim) return nullptr;
    g_sim = new Sim();
    Sim* s = g_sim;
    s->top->clk = 0;
    s->top->rst_n = 0;
    s->top->i_trace_mask = 0xFFFFFFFFu;
    s->top->m_trace_tready = 1;
    s->top->s_axil_awvalid = 0;
    s->top->s_axil_wvalid = 0;
    s->top->s_axil_arvalid = 0;
    s->top->s_axil_bready = 0;
    s->top->s_axil_rready = 0;
    s->top->eval();
    for (int i = 0; i < 4; ++i) {
        std::string base = "gen_sm[" + std::to_string(i) + "].u_sm.";
        s->sm_pc[i] = lookup(s, (base + "pc").c_str());
        s->sm_stall[i] = lookup(s, (base + "stalled").c_str());
    }
    return s;
}

void pio2sim_destroy(void* h) {
    if (h && h == g_sim) {
        delete g_sim;
        g_sim = nullptr;
    }
}

void pio2sim_reset(void* h, int cycles) {
    Sim* s = static_cast<Sim*>(h);
    s->top->rst_n = 0;
    for (int i = 0; i < cycles; ++i) s->step();
    s->top->rst_n = 1;
    s->top->eval();
}

uint64_t pio2sim_cycle(void* h) { return static_cast<Sim*>(h)->cycle; }

void pio2sim_set_ext(void* h, uint32_t value, uint32_t mask, uint32_t pull) {
    Sim* s = static_cast<Sim*>(h);
    s->ext = value;
    s->ext_mask = mask;
    s->pull = pull;
    s->top->i_gpio = s->pads();
    s->top->eval();
}

uint32_t pio2sim_gpio_out(void* h) { return static_cast<Sim*>(h)->top->o_gpio_out; }
uint32_t pio2sim_gpio_dir(void* h) { return static_cast<Sim*>(h)->pad_dir(); }
uint32_t pio2sim_gpio_in(void* h) { return static_cast<Sim*>(h)->pads(); }

// Run n clocks (recorded in the pending queue when recording is on).
void pio2sim_run(void* h, uint32_t n) {
    Sim* s = static_cast<Sim*>(h);
    for (uint32_t i = 0; i < n; ++i) s->step();
}

void pio2sim_record(void* h, int on) {
    Sim* s = static_cast<Sim*>(h);
    s->record = on != 0;
    if (!s->record) s->pending.clear();
}

uint32_t pio2sim_pending(void* h) { return static_cast<uint32_t>(static_cast<Sim*>(h)->pending.size()); }

// Copy the n oldest recorded clocks out (pcs and stalls: 4 bytes per clock) and drop them.
void pio2sim_drain(void* h, uint32_t n, uint32_t* out, uint32_t* dir, uint32_t* in, uint8_t* pcs,
                   uint8_t* stalls) {
    Sim* s = static_cast<Sim*>(h);
    n = n < s->pending.size() ? n : static_cast<uint32_t>(s->pending.size());
    for (uint32_t i = 0; i < n; ++i) {
        const auto& r = s->pending[i];
        out[i] = r.out;
        dir[i] = r.dir;
        in[i] = r.in;
        std::memcpy(pcs + 4 * i, r.pc, 4);
        std::memcpy(stalls + 4 * i, r.stall, 4);
    }
    s->pending.erase(s->pending.begin(), s->pending.begin() + n);
}

// AXI4-Lite write of one register (offset from the tile base). Returns BRESP, or -1 on timeout.
int pio2sim_write(void* h, uint32_t offset, uint32_t value) {
    Sim* s = static_cast<Sim*>(h);
    auto* t = s->top;
    t->s_axil_awaddr = kBase + offset;
    t->s_axil_awvalid = 1;
    t->s_axil_wdata = value;
    t->s_axil_wstrb = 0xF;
    t->s_axil_wvalid = 1;
    t->s_axil_bready = 1;
    t->eval();
    for (int i = 0; i < 64; ++i) {
        const bool awr = t->s_axil_awready && t->s_axil_awvalid;
        const bool wr = t->s_axil_wready && t->s_axil_wvalid;
        const bool bv = t->s_axil_bvalid;
        const int resp = t->s_axil_bresp;
        s->step();
        if (awr) t->s_axil_awvalid = 0;
        if (wr) t->s_axil_wvalid = 0;
        if (bv) {
            t->s_axil_bready = 0;
            t->eval();
            return resp;
        }
        t->eval();
    }
    return -1;
}

// AXI4-Lite read. Returns RRESP (data in *value), or -1 on timeout.
int pio2sim_read(void* h, uint32_t offset, uint32_t* value) {
    Sim* s = static_cast<Sim*>(h);
    auto* t = s->top;
    t->s_axil_araddr = kBase + offset;
    t->s_axil_arvalid = 1;
    t->s_axil_rready = 1;
    t->eval();
    for (int i = 0; i < 64; ++i) {
        const bool arr = t->s_axil_arready && t->s_axil_arvalid;
        const bool rv = t->s_axil_rvalid;
        const uint32_t data = t->s_axil_rdata;
        const int resp = t->s_axil_rresp;
        s->step();
        if (arr) t->s_axil_arvalid = 0;
        if (rv) {
            t->s_axil_rready = 0;
            t->eval();
            *value = data;
            return resp;
        }
        t->eval();
    }
    return -1;
}

// Signal by hierarchical name below the tile, e.g. "gen_sm[0].u_sm.reg_x". Returns the width in
// bits, or -1 if no such signal.
int pio2sim_get(void* h, const char* name, uint32_t* words, int nwords) {
    return read_handle(lookup(static_cast<Sim*>(h), name), words, nwords);
}

// Element `index` of an unpacked array, e.g. ("iram", 3).
int pio2sim_get_index(void* h, const char* name, int index, uint32_t* words, int nwords) {
    vpiHandle a = lookup(static_cast<Sim*>(h), name);
    if (!a) return -1;
    return read_handle(vpi_handle_by_index(a, index), words, nwords);
}

// Trace records ([SPEC-TRC-01] layout) taken from the core since the last drain.
uint32_t pio2sim_events_pending(void* h) {
    return static_cast<uint32_t>(static_cast<Sim*>(h)->events.size());
}

void pio2sim_events_drain(void* h, uint32_t n, uint64_t* out) {
    Sim* s = static_cast<Sim*>(h);
    n = n < s->events.size() ? n : static_cast<uint32_t>(s->events.size());
    std::memcpy(out, s->events.data(), n * sizeof(uint64_t));
    s->events.erase(s->events.begin(), s->events.begin() + n);
}

void pio2sim_trace_config(void* h, uint32_t type_mask, int ready) {
    Sim* s = static_cast<Sim*>(h);
    s->top->i_trace_mask = type_mask;
    s->top->m_trace_tready = ready ? 1 : 0;
    s->top->eval();
}

void pio2sim_trace_counters(void* h, uint32_t* dropped, uint32_t* merged) {
    Sim* s = static_cast<Sim*>(h);
    *dropped = s->top->o_trace_dropped;
    *merged = s->top->o_trace_merged;
}

// Lines whose output enable is inverted at the pad (RP2040 gpio_set_oeover INVERT, used by the
// pico-examples I2C program for open-drain). Default 0: the pad loop is unchanged.
void pio2sim_set_oe_invert(void* h, uint32_t mask) {
    Sim* s = static_cast<Sim*>(h);
    s->oe_inv = mask;
    s->top->i_gpio = s->pads();
    s->top->eval();
}

// Snapshot of the whole model (Verilator --savable) plus the wrapper's own state, into an internal
// buffer; returns its size, pio2sim_snapshot_copy copies it out ([SPEC-DBG-06]). Recorded clocks and
// trace records not yet drained are not part of a snapshot (the caller drains first).
static std::vector<uint8_t> g_snap;

uint32_t pio2sim_snapshot(void* h) {
    Sim* s = static_cast<Sim*>(h);
    MemSave os;
    static_cast<VerilatedSerialize&>(os) << &s->ctx << *s->top;
    os.write(&s->cycle, sizeof s->cycle);
    os.write(&s->ext, sizeof s->ext);
    os.write(&s->ext_mask, sizeof s->ext_mask);
    os.write(&s->pull, sizeof s->pull);
    os.write(&s->oe_inv, sizeof s->oe_inv);
    os.finish();
    g_snap.swap(os.data);
    return static_cast<uint32_t>(g_snap.size());
}

void pio2sim_snapshot_copy(void* h, uint8_t* out) {
    (void)h;
    std::memcpy(out, g_snap.data(), g_snap.size());
}

// Restore a snapshot made by pio2sim_snapshot (same library build). Drops undrained records.
void pio2sim_restore(void* h, const uint8_t* data, uint32_t n) {
    Sim* s = static_cast<Sim*>(h);
    MemRestore is(data, n);
    static_cast<VerilatedDeserialize&>(is) >> &s->ctx >> *s->top;
    is.read(&s->cycle, sizeof s->cycle);
    is.read(&s->ext, sizeof s->ext);
    is.read(&s->ext_mask, sizeof s->ext_mask);
    is.read(&s->pull, sizeof s->pull);
    is.read(&s->oe_inv, sizeof s->oe_inv);
    is.finish();
    s->pending.clear();
    s->events.clear();
    s->top->eval();
}

}  // extern "C"
