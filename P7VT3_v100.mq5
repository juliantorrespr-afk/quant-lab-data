//+------------------------------------------------------------------+
//|                                                        P7VT3.mq5 |
//|  P7-VT3 daily trend + vol-target book, for a funded CFD account. |
//|                                                                  |
//|  Strategy (never tuned; identical to lab sim3.py):               |
//|    US100 / BTCUSD : long when D1 close[1] > SMA150(close), else  |
//|                     flat.  XAUUSD : always long.                 |
//|    Size   : equity x weight x min(1, targetVol/realisedVol20)    |
//|             x Exposure.  Weights .40/.20/.40, targets .15/.40/.15|
//|    Cadence: signal read once per closed D1 bar (index [1]);      |
//|             flips act on the NEXT bar (>=1 day lag, always);     |
//|             vol scalar re-read Mondays; full rebalance in the    |
//|             first 3 weekdays of each month; otherwise hold.      |
//|             5%-of-book drift band on Monday resizes.             |
//|                                                                  |
//|  Rails (hard-coded, not inputs):                                 |
//|    - broker-side SL at -15% from entry on every position         |
//|    - daily-loss guard: intraday equity <= -4.0% of day-start ->  |
//|      flatten all, halt until next server day                     |
//|    - static max-loss guard: equity <= InitialBalance x 0.92 ->   |
//|      flatten all, halt PERMANENTLY until manual reset            |
//|    - no single order > 45% of equity in notional (refused)       |
//|    - no orders while a symbol's trade session is closed          |
//|                                                                  |
//|  Go-live gate: LiveTrading=false (default) computes and logs the |
//|  exact orders it WOULD send, and sends nothing.                  |
//|                                                                  |
//|  Attach to ONE chart, any symbol, D1.  The EA reads the other    |
//|  symbols itself.  Runs on a 30-second timer, not on ticks.       |
//+------------------------------------------------------------------+
#property copyright   "QUANT_LAB"
#property version     "1.00"

#include <Trade\Trade.mqh>

//=================================================================
// INPUTS
//=================================================================
input string  SymUS100      = "US100";     // QQQ proxy (US100 / NAS100 / USTEC / NDX100)
input string  SymBTC        = "BTCUSD";    // Bitcoin
input string  SymGold       = "XAUUSD";    // Gold
input double  Exposure      = 0.55;        // venue exposure scalar (lab: 0.55 = 20% headroom)
input bool    LiveTrading   = false;       // FALSE = log only, send nothing.  YOU flip this.
input bool    ImmediateFlips= true;        // true: SMA flip acts next bar (spec). false: waits for Monday (lab-calibrated variant, see README)
input long    MagicNumber   = 773003;      // identifies this EA's positions
input int     SlippagePts   = 50;          // max deviation in points for market orders

//=================================================================
// STRATEGY CONSTANTS (never tuned)
//=================================================================
#define N_SLEEVES      3
#define SMA_LEN        150
#define VOL_LEN        20
#define TIMER_SEC      30

// rails
#define DISASTER_STOP      0.15    // SL 15% below entry, broker-side
#define DAILY_LOSS_HALT    0.040   // halt for the day at -4.0% of day-start equity (firm limit -5%)
#define STATIC_LOSS_HALT   0.080   // halt permanently at -8.0% of InitialBalance (firm floor -10%)
#define MAX_ORDER_FRAC     0.45    // refuse any single order > 45% of equity (notional)
#define DRIFT_BAND         0.05    // Monday resize only if |delta| > 5% of equity
#define MIN_TRADE_FRAC     0.005   // month-start rebalance: skip deltas < 0.5% of equity

// global-variable keys (persist across restarts)
#define GV_INITBAL         "P7VT3_InitialBalance"
#define GV_DAYSTART_EQ     "P7VT3_DayStartEquity"
#define GV_DAYSTART_DATE   "P7VT3_DayStartDate"
#define GV_HALT_DAY        "P7VT3_HaltDay"        // server day (unix) when the daily halt fired
#define GV_HALT_PERM       "P7VT3_HaltPermanent"  // 1 = halted until YOU delete this variable
#define LOG_FILE           "P7VT3_log.csv"
#define STATE_FILE         "P7VT3_state.csv"

//=================================================================
// PER-SLEEVE STATE
//=================================================================
struct Sleeve
{
   string   sym;
   double   weight;
   double   targetVol;
   double   annFactor;      // 252 (5-day markets) or 365 (BTC)
   bool     timed;          // SMA filter applies?
   // last computed
   datetime lastBar;        // time of bar[1] last processed
   double   close1;
   double   sma;
   double   vol;
   double   scalarFresh;    // min(1, tv/vol) from today's bar
   double   scalarInUse;    // re-read on Mondays only
   bool     sigFresh;       // close > sma today
   bool     sigInUse;       // signal currently driving the position
   bool     pendingFlip;    // flip observed, waiting for execution (ImmediateFlips=false: waits for Monday)
   // pending order (queued until market open)
   double   pendingLots;    // signed: +buy, -sell; 0 = nothing queued
   string   pendingReason;
   // dry-run virtual position
   double   virtLots;
};

Sleeve   S[N_SLEEVES];
CTrade   trade;

datetime g_lastServerDay = 0;
bool     g_initialised   = false;

//=================================================================
// UTILITIES
//=================================================================
datetime DayFloor(datetime t) { return (datetime)((long)t - ((long)t % 86400)); }

int Weekday(datetime t) { MqlDateTime d; TimeToStruct(t, d); return d.day_of_week; } // 0=Sun..6=Sat

bool IsFirst3Weekdays(datetime t)
{
   MqlDateTime d; TimeToStruct(t, d);
   if(d.day_of_week == 0 || d.day_of_week == 6) return false;
   int count = 0;
   for(int day = 1; day <= d.day; day++)
   {
      MqlDateTime x = d; x.day = day; x.hour = 12; x.min = 0; x.sec = 0;
      datetime tt = StructToTime(x);
      int wd = Weekday(tt);
      if(wd != 0 && wd != 6) count++;
   }
   return count <= 3;
}

void Log(string msg)
{
   Print("[P7VT3] ", msg);
}

void LogCSV(string sleeve, double close1, double sma, double vol, double scalar,
            double targetLots, string action, double equity)
{
   int h = FileOpen(LOG_FILE, FILE_READ|FILE_WRITE|FILE_CSV|FILE_ANSI, ',');
   if(h == INVALID_HANDLE) { Log("CSV open failed err=" + IntegerToString(GetLastError())); return; }
   if(FileSize(h) == 0)
      FileWrite(h, "server_time", "sleeve", "close", "sma150", "vol20_ann", "scalar", "target_lots", "action", "equity", "live");
   FileSeek(h, 0, SEEK_END);
   FileWrite(h, TimeToString(TimeTradeServer(), TIME_DATE|TIME_MINUTES), sleeve,
             DoubleToString(close1, 2), DoubleToString(sma, 2), DoubleToString(vol, 4),
             DoubleToString(scalar, 3), DoubleToString(targetLots, 2), action,
             DoubleToString(equity, 2), LiveTrading ? "LIVE" : "DRY");
   FileClose(h);
}

// persist per-sleeve state so a restart does not lose the in-use signal/scalar
void SaveState()
{
   int h = FileOpen(STATE_FILE, FILE_WRITE|FILE_CSV|FILE_ANSI, ',');
   if(h == INVALID_HANDLE) return;
   for(int i = 0; i < N_SLEEVES; i++)
      FileWrite(h, S[i].sym, (long)S[i].lastBar, S[i].scalarInUse, S[i].sigInUse ? 1 : 0,
                S[i].pendingFlip ? 1 : 0, S[i].virtLots);
   FileClose(h);
}

void LoadState()
{
   int h = FileOpen(STATE_FILE, FILE_READ|FILE_CSV|FILE_ANSI, ',');
   if(h == INVALID_HANDLE) return;
   while(!FileIsEnding(h))
   {
      string sym = FileReadString(h); if(sym == "") break;
      long   lb  = (long)FileReadNumber(h);
      double sc  = FileReadNumber(h);
      int    sg  = (int)FileReadNumber(h);
      int    pf  = (int)FileReadNumber(h);
      double vl  = FileReadNumber(h);
      for(int i = 0; i < N_SLEEVES; i++)
         if(S[i].sym == sym)
         {
            S[i].lastBar = (datetime)lb; S[i].scalarInUse = sc;
            S[i].sigInUse = (sg == 1); S[i].pendingFlip = (pf == 1); S[i].virtLots = vl;
         }
   }
   FileClose(h);
}

//=================================================================
// MARKET SESSION CHECK
//   Uses the broker's published trade sessions.  from/to are seconds
//   from the start of the server day (MQL5 convention; 'to' may exceed
//   86400 for sessions that run past midnight).  If the broker publishes
//   no sessions at all we assume open and let the server reject.
//   NOTE: session-table accuracy is broker-dependent; verify on FTMO.
//=================================================================
bool IsMarketOpen(string sym)
{
   datetime now = TimeTradeServer();
   datetime day0 = DayFloor(now);
   long secs = (long)now - (long)day0;
   ENUM_DAY_OF_WEEK wd = (ENUM_DAY_OF_WEEK)Weekday(now);
   datetime from, to;
   bool any = false;
   for(int i = 0; SymbolInfoSessionTrade(sym, wd, i, from, to); i++)
   {
      any = true;
      if(secs >= (long)from && secs < (long)to) return true;
   }
   if(!any) return true;
   // also allow a session from the previous day that spills past midnight
   ENUM_DAY_OF_WEEK ywd = (ENUM_DAY_OF_WEEK)((Weekday(now) + 6) % 7);
   for(int i = 0; SymbolInfoSessionTrade(sym, ywd, i, from, to); i++)
      if((long)to > 86400 && secs < (long)to - 86400) return true;
   return false;
}

//=================================================================
// LOT MATH  — the place EAs silently go wrong, so it is spelled out.
//
//   We want a position worth `notional` units of ACCOUNT currency.
//   For one lot, a 1.0 move in price changes P&L by
//        perPoint = SYMBOL_TRADE_TICK_VALUE / SYMBOL_TRADE_TICK_SIZE
//   (tick value is already in account currency, so this handles any
//   quote/account currency mismatch).  Therefore the notional of one
//   lot at the current price is
//        notionalPerLot = price * perPoint
//   and       lots = notional / notionalPerLot
//   rounded DOWN to SYMBOL_VOLUME_STEP and clamped to [VOLUME_MIN, VOLUME_MAX].
//   Below VOLUME_MIN we return 0 (do not trade) rather than rounding up.
//
//   Cross-check printed to the log: notionalPerLot should equal
//   SYMBOL_TRADE_CONTRACT_SIZE * price when quote ccy == account ccy.
//   e.g. FTMO-style specs (VERIFY ON YOUR ACCOUNT):
//        US100  contract 1   @ 20,000 -> 1 lot ~ $20,000
//        XAUUSD contract 100 @  3,300 -> 1 lot ~ $330,000  (coarse! 0.01 lot = $3,300)
//        BTCUSD contract 1   @ 80,000 -> 1 lot ~ $80,000
//=================================================================
double NotionalPerLot(string sym)
{
   double tv   = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_VALUE);
   double ts   = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_SIZE);
   double bid  = SymbolInfoDouble(sym, SYMBOL_BID);
   if(tv <= 0 || ts <= 0 || bid <= 0) return 0;
   return bid * tv / ts;
}

double NotionalToLots(string sym, double notional)
{
   double npl = NotionalPerLot(sym);
   if(npl <= 0) return 0;
   double raw  = notional / npl;
   double step = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);
   double vmin = SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN);
   double vmax = SymbolInfoDouble(sym, SYMBOL_VOLUME_MAX);
   if(step <= 0) step = 0.01;
   double lots = MathFloor(raw / step + 1e-9) * step;     // round DOWN
   if(lots < vmin) return 0;
   if(lots > vmax) lots = vmax;
   return NormalizeDouble(lots, 8);
}

double LotsToNotional(string sym, double lots) { return lots * NotionalPerLot(sym); }

//=================================================================
// POSITION BOOK-KEEPING (by symbol + magic).  Works in hedging and
// netting mode: we only ever hold BUY positions, and we reduce by
// closing tickets partially/fully until the reduction is met.
//=================================================================
double HeldLots(string sym)
{
   if(!LiveTrading)
      for(int i = 0; i < N_SLEEVES; i++) if(S[i].sym == sym) return S[i].virtLots;
   double v = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong tk = PositionGetTicket(i);
      if(tk == 0 || !PositionSelectByTicket(tk)) continue;
      if(PositionGetString(POSITION_SYMBOL) != sym) continue;
      if(PositionGetInteger(POSITION_MAGIC) != MagicNumber) continue;
      if(PositionGetInteger(POSITION_TYPE) != POSITION_TYPE_BUY) continue;
      v += PositionGetDouble(POSITION_VOLUME);
   }
   return v;
}

double StopLossPrice(string sym, double entry)
{
   int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   double sl = NormalizeDouble(entry * (1.0 - DISASTER_STOP), digits);
   // respect the broker's minimum stop distance (STOPS_LEVEL in points)
   double pt = SymbolInfoDouble(sym, SYMBOL_POINT);
   long   stopsLevel = SymbolInfoInteger(sym, SYMBOL_TRADE_STOPS_LEVEL);
   double bid = SymbolInfoDouble(sym, SYMBOL_BID);
   double maxSL = bid - stopsLevel * pt;
   if(sl > maxSL) sl = NormalizeDouble(maxSL, digits);
   return sl;
}

bool OpenLong(int i, double lots, string reason)
{
   string sym = S[i].sym;
   double eq  = AccountInfoDouble(ACCOUNT_EQUITY);
   double notional = LotsToNotional(sym, lots);
   if(notional > MAX_ORDER_FRAC * eq)
   {
      Log(StringFormat("REFUSED %s BUY %.2f lots = %.0f notional > %.0f%% of equity %.0f. CHECK DATA.",
                       sym, lots, notional, MAX_ORDER_FRAC*100, eq));
      LogCSV(sym, S[i].close1, S[i].sma, S[i].vol, S[i].scalarInUse, lots, "REFUSED_45PCT", eq);
      return false;
   }
   double ask = SymbolInfoDouble(sym, SYMBOL_ASK);
   double sl  = StopLossPrice(sym, ask);
   if(!LiveTrading)
   {
      Log(StringFormat("DRY  WOULD BUY %s %.2f lots (~%.0f) SL %.2f  [%s]", sym, lots, notional, sl, reason));
      S[i].virtLots += lots;
      LogCSV(sym, S[i].close1, S[i].sma, S[i].vol, S[i].scalarInUse, lots, "DRY_BUY:" + reason, eq);
      return true;
   }
   trade.SetTypeFillingBySymbol(sym);
   bool ok = trade.Buy(lots, sym, 0.0, sl, 0.0, "P7VT3 " + reason);
   uint rc = trade.ResultRetcode();
   Log(StringFormat("LIVE BUY %s %.2f lots (~%.0f) SL %.2f [%s] -> retcode %u %s",
                    sym, lots, notional, sl, reason, rc, trade.ResultRetcodeDescription()));
   LogCSV(sym, S[i].close1, S[i].sma, S[i].vol, S[i].scalarInUse, lots,
          (ok ? "BUY:" : "BUY_FAILED:") + reason + " rc=" + IntegerToString(rc), eq);
   return ok;
}

bool ReduceLong(int i, double lots, string reason)
{
   string sym = S[i].sym;
   double eq  = AccountInfoDouble(ACCOUNT_EQUITY);
   double notional = LotsToNotional(sym, lots);
   if(notional > MAX_ORDER_FRAC * eq)
   {
      Log(StringFormat("REFUSED %s SELL %.2f lots = %.0f notional > %.0f%% of equity. CHECK DATA.",
                       sym, lots, notional, MAX_ORDER_FRAC*100));
      LogCSV(sym, S[i].close1, S[i].sma, S[i].vol, S[i].scalarInUse, -lots, "REFUSED_45PCT", eq);
      return false;
   }
   if(!LiveTrading)
   {
      Log(StringFormat("DRY  WOULD SELL %s %.2f lots (~%.0f)  [%s]", sym, lots, notional, reason));
      S[i].virtLots = MathMax(0.0, S[i].virtLots - lots);
      LogCSV(sym, S[i].close1, S[i].sma, S[i].vol, S[i].scalarInUse, -lots, "DRY_SELL:" + reason, eq);
      return true;
   }
   trade.SetTypeFillingBySymbol(sym);
   double remaining = lots;
   double step = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);
   bool allok = true;
   for(int k = PositionsTotal() - 1; k >= 0 && remaining > step/2; k--)
   {
      ulong tk = PositionGetTicket(k);
      if(tk == 0 || !PositionSelectByTicket(tk)) continue;
      if(PositionGetString(POSITION_SYMBOL) != sym) continue;
      if(PositionGetInteger(POSITION_MAGIC) != MagicNumber) continue;
      if(PositionGetInteger(POSITION_TYPE) != POSITION_TYPE_BUY) continue;
      double pv = PositionGetDouble(POSITION_VOLUME);
      bool ok;
      if(pv <= remaining + step/2) { ok = trade.PositionClose(tk, SlippagePts); remaining -= pv; }
      else { ok = trade.PositionClosePartial(tk, NormalizeDouble(remaining, 8), SlippagePts); remaining = 0; }
      Log(StringFormat("LIVE SELL %s ticket %I64u -> retcode %u %s", sym, tk, trade.ResultRetcode(), trade.ResultRetcodeDescription()));
      allok = allok && ok;
   }
   LogCSV(sym, S[i].close1, S[i].sma, S[i].vol, S[i].scalarInUse, -lots,
          (allok ? "SELL:" : "SELL_PARTIAL_FAIL:") + reason, eq);
   return allok;
}

void FlattenAll(string why)
{
   Log("FLATTEN ALL: " + why);
   for(int i = 0; i < N_SLEEVES; i++)
   {
      double held = HeldLots(S[i].sym);
      S[i].pendingLots = 0; S[i].pendingReason = "";
      if(held > 0)
      {
         if(IsMarketOpen(S[i].sym)) ReduceLong(i, held, "FLATTEN:" + why);
         else { S[i].pendingLots = -held; S[i].pendingReason = "FLATTEN:" + why;
                Log("  " + S[i].sym + " market closed - flatten queued"); }
      }
   }
   SaveState();
}

//=================================================================
// RAILS
//=================================================================
bool PermHalted() { return GlobalVariableCheck(GV_HALT_PERM) && GlobalVariableGet(GV_HALT_PERM) > 0.5; }

bool DayHalted()
{
   if(!GlobalVariableCheck(GV_HALT_DAY)) return false;
   return (long)GlobalVariableGet(GV_HALT_DAY) == (long)DayFloor(TimeTradeServer());
}

void CheckRails()
{
   double eq = AccountInfoDouble(ACCOUNT_EQUITY);
   // --- static max-loss (permanent) ---
   double initBal = GlobalVariableGet(GV_INITBAL);
   if(initBal > 0 && eq <= initBal * (1.0 - STATIC_LOSS_HALT) && !PermHalted())
   {
      GlobalVariableSet(GV_HALT_PERM, 1.0);
      Log(StringFormat("!!!!! STATIC MAX-LOSS GUARD: equity %.2f <= %.2f (InitialBalance %.2f x %.2f). PERMANENT HALT. "
                       "Delete global variable %s to reset, after human review.", eq, initBal*(1-STATIC_LOSS_HALT), initBal, 1-STATIC_LOSS_HALT, GV_HALT_PERM));
      LogCSV("BOOK", 0, 0, 0, 0, 0, "STATIC_MAXLOSS_HALT", eq);
      FlattenAll("static max-loss guard");
      Alert("P7VT3: STATIC MAX-LOSS HALT. Everything flattened. Human review required.");
      return;
   }
   // --- daily-loss (halt for the server day) ---
   double dayEq = GlobalVariableGet(GV_DAYSTART_EQ);
   if(dayEq > 0 && eq <= dayEq * (1.0 - DAILY_LOSS_HALT) && !DayHalted())
   {
      GlobalVariableSet(GV_HALT_DAY, (double)(long)DayFloor(TimeTradeServer()));
      Log(StringFormat("!!!!! DAILY-LOSS GUARD: equity %.2f <= %.2f (day start %.2f x %.3f). Flattening, halted until next server day.",
                       eq, dayEq*(1-DAILY_LOSS_HALT), dayEq, 1-DAILY_LOSS_HALT));
      LogCSV("BOOK", 0, 0, 0, 0, 0, "DAILY_LOSS_HALT", eq);
      FlattenAll("daily-loss guard");
      Alert("P7VT3: DAILY-LOSS HALT. Flattened for today.");
   }
}

void RollServerDay()
{
   datetime today = DayFloor(TimeTradeServer());
   if(today == g_lastServerDay) return;
   g_lastServerDay = today;
   // Restart mid-day: keep the anchor already recorded for today.  Re-anchoring to
   // current equity after a loss would silently loosen the daily guard.
   if(GlobalVariableCheck(GV_DAYSTART_DATE) && (long)GlobalVariableGet(GV_DAYSTART_DATE) == (long)today
      && GlobalVariableGet(GV_DAYSTART_EQ) > 0)
   {
      Log(StringFormat("Restart within server day %s: keeping day-start equity anchor %.2f",
                       TimeToString(today, TIME_DATE), GlobalVariableGet(GV_DAYSTART_EQ)));
      return;
   }
   double eq = AccountInfoDouble(ACCOUNT_EQUITY);
   // Day-start equity anchor for the daily-loss guard.
   // ASSUMPTION: FTMO's daily-loss window resets at midnight CE(S)T and FTMO's MT5
   // server clock is CE(S)T, so server-day == firm-day.  VERIFY on your account.
   GlobalVariableSet(GV_DAYSTART_EQ, eq);
   GlobalVariableSet(GV_DAYSTART_DATE, (double)(long)today);
   Log(StringFormat("New server day %s  day-start equity %.2f  (daily halt at %.2f)",
                    TimeToString(today, TIME_DATE), eq, eq*(1-DAILY_LOSS_HALT)));
}

//=================================================================
// SIGNAL
//   CopyClose(sym, D1, start_pos=1, count=SMA_LEN+1) -> bars [1..151].
//   With ArraySetAsSeries(true): c[0] = bar[1] (last CLOSED bar),
//   c[1] = bar[2], ... .  Bar [0] (the forming bar) is never touched.
//=================================================================
bool ComputeSignal(int i)
{
   string sym = S[i].sym;
   double c[];
   ArraySetAsSeries(c, true);
   int need = SMA_LEN + 1;
   int got = CopyClose(sym, PERIOD_D1, 1, need, c);
   if(got < need) { Log(sym + ": only " + IntegerToString(got) + " D1 bars, need " + IntegerToString(need)); return false; }
   double sum = 0;
   for(int k = 0; k < SMA_LEN; k++) sum += c[k];
   S[i].sma    = sum / SMA_LEN;
   S[i].close1 = c[0];
   // 20 daily simple returns from the last 21 closes; sample std (ddof=1)
   double r[VOL_LEN]; double mean = 0;
   for(int k = 0; k < VOL_LEN; k++) { r[k] = c[k] / c[k+1] - 1.0; mean += r[k]; }
   mean /= VOL_LEN;
   double ss = 0;
   for(int k = 0; k < VOL_LEN; k++) ss += (r[k]-mean)*(r[k]-mean);
   double sd = MathSqrt(ss / (VOL_LEN - 1));
   S[i].vol = sd * MathSqrt(S[i].annFactor);
   S[i].scalarFresh = (S[i].vol > 0) ? MathMin(1.0, S[i].targetVol / S[i].vol) : 1.0;
   S[i].sigFresh = S[i].timed ? (S[i].close1 > S[i].sma) : true;
   return true;
}

//=================================================================
// DECISION for one sleeve, on its new D1 bar
//=================================================================
void OnNewBar(int i)
{
   string sym = S[i].sym;
   if(!ComputeSignal(i)) return;
   datetime now = TimeTradeServer();
   bool monday = (Weekday(now) == 1);
   bool month3 = IsFirst3Weekdays(now);
   double eq   = AccountInfoDouble(ACCOUNT_EQUITY);

   // first ever run: adopt fresh values
   if(S[i].scalarInUse <= 0) { S[i].scalarInUse = S[i].scalarFresh; S[i].sigInUse = S[i].sigFresh; }

   // Monday: re-read the vol scalar
   if(monday) S[i].scalarInUse = S[i].scalarFresh;

   // Flip detection.  The signal was formed on bar[1]; we are now on bar[0],
   // i.e. one bar later.  That is the >=1-day lag.
   bool flipNow = false;
   if(S[i].sigFresh != S[i].sigInUse)
   {
      if(ImmediateFlips || monday) { S[i].sigInUse = S[i].sigFresh; flipNow = true; S[i].pendingFlip = false; }
      else { S[i].pendingFlip = true; Log(sym + ": SMA flip observed, waiting for Monday (ImmediateFlips=false)"); }
   }
   else S[i].pendingFlip = false;

   // target
   bool   on        = S[i].sigInUse;
   double tgtNot    = on ? eq * S[i].weight * S[i].scalarInUse * Exposure : 0.0;
   double tgtLots   = on ? NotionalToLots(sym, tgtNot) : 0.0;
   double held      = HeldLots(sym);
   double deltaLots = tgtLots - held;
   double deltaNot  = LotsToNotional(sym, MathAbs(deltaLots));
   double step      = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);

   string why = ""; bool act = false;
   if(flipNow)                                         { why = on ? "FLIP_ON" : "FLIP_OFF"; act = true; }
   else if(month3 && deltaNot > MIN_TRADE_FRAC * eq)   { why = "MONTH_REBAL"; act = true; }
   else if(monday && deltaNot > DRIFT_BAND * eq)       { why = "MONDAY_RESIZE"; act = true; }
   if(MathAbs(deltaLots) < step/2) act = false;

   Log(StringFormat("%s bar %s | close %.2f sma %.2f -> %s | vol %.1f%% scalar %.3f (in use %.3f) | target %.2f lots (~%.0f) held %.2f | %s",
       sym, TimeToString(iTime(sym, PERIOD_D1, 1), TIME_DATE), S[i].close1, S[i].sma, on ? "LONG" : "FLAT",
       S[i].vol*100, S[i].scalarFresh, S[i].scalarInUse, tgtLots, tgtNot, held, act ? why : "HOLD"));
   Log(StringFormat("   lot math %s: notional/lot %.2f (contract %.0f x bid %.2f = %.2f) step %.2f min %.2f",
       sym, NotionalPerLot(sym), SymbolInfoDouble(sym, SYMBOL_TRADE_CONTRACT_SIZE), SymbolInfoDouble(sym, SYMBOL_BID),
       SymbolInfoDouble(sym, SYMBOL_TRADE_CONTRACT_SIZE)*SymbolInfoDouble(sym, SYMBOL_BID),
       step, SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN)));
   LogCSV(sym, S[i].close1, S[i].sma, S[i].vol, S[i].scalarInUse, tgtLots, act ? why : "HOLD", eq);

   S[i].pendingLots = act ? deltaLots : 0.0;
   S[i].pendingReason = why;
   S[i].lastBar = iTime(sym, PERIOD_D1, 1);
   SaveState();
}

void ProcessPending(int i)
{
   if(MathAbs(S[i].pendingLots) < 1e-9) return;
   if(PermHalted() || DayHalted()) { S[i].pendingLots = 0; return; }
   if(!IsMarketOpen(S[i].sym)) return;          // retry on the next timer tick
   double d = S[i].pendingLots; string why = S[i].pendingReason;
   S[i].pendingLots = 0; S[i].pendingReason = "";
   if(d > 0) OpenLong(i, d, why); else ReduceLong(i, -d, why);
   SaveState();
}

//=================================================================
// EVENTS
//=================================================================
int OnInit()
{
   S[0].sym = SymUS100; S[0].weight = 0.40; S[0].targetVol = 0.15; S[0].annFactor = 252; S[0].timed = true;
   S[1].sym = SymBTC;   S[1].weight = 0.20; S[1].targetVol = 0.40; S[1].annFactor = 365; S[1].timed = true;
   S[2].sym = SymGold;  S[2].weight = 0.40; S[2].targetVol = 0.15; S[2].annFactor = 252; S[2].timed = false;
   for(int i = 0; i < N_SLEEVES; i++)
   {
      S[i].lastBar = 0; S[i].scalarInUse = 0; S[i].sigInUse = false; S[i].pendingFlip = false;
      S[i].pendingLots = 0; S[i].virtLots = 0;
      if(!SymbolSelect(S[i].sym, true))
      { Log("SYMBOL NOT FOUND: " + S[i].sym + " - fix the input name."); return INIT_FAILED; }
      if(SymbolInfoString(S[i].sym, SYMBOL_CURRENCY_PROFIT) != AccountInfoString(ACCOUNT_CURRENCY))
         Log("NOTE " + S[i].sym + " profit currency " + SymbolInfoString(S[i].sym, SYMBOL_CURRENCY_PROFIT) +
             " != account currency " + AccountInfoString(ACCOUNT_CURRENCY) + ": lot math uses tick value (handles this), verify once by hand.");
   }
   LoadState();
   trade.SetExpertMagicNumber(MagicNumber);
   trade.SetDeviationInPoints(SlippagePts);

   if(!GlobalVariableCheck(GV_INITBAL) || GlobalVariableGet(GV_INITBAL) <= 0)
   {
      double bal = AccountInfoDouble(ACCOUNT_BALANCE);
      GlobalVariableSet(GV_INITBAL, bal);
      Log(StringFormat("InitialBalance recorded = %.2f (static max-loss halt at %.2f). "
                       "NOTE: MT5 global variables expire after 4 weeks without access; this EA touches it daily.", bal, bal*(1-STATIC_LOSS_HALT)));
   }
   Log(StringFormat("Init OK. Exposure %.2f  LiveTrading=%s  ImmediateFlips=%s  Magic %I64d  InitialBalance %.2f  MarginMode %s",
       Exposure, LiveTrading ? "TRUE" : "false", ImmediateFlips ? "true" : "false", MagicNumber, GlobalVariableGet(GV_INITBAL),
       AccountInfoInteger(ACCOUNT_MARGIN_MODE) == ACCOUNT_MARGIN_MODE_RETAIL_HEDGING ? "HEDGING" : "NETTING"));
   if(PermHalted()) Log("!!!!! PERMANENT HALT FLAG IS SET. EA will not trade until " + GV_HALT_PERM + " is deleted.");
   if(!LiveTrading) Log("DRY RUN: nothing will be sent. Read the log for two weeks, then set LiveTrading=true yourself.");
   EventSetTimer(TIMER_SEC);
   g_initialised = true;
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason) { EventKillTimer(); SaveState(); }

void OnTick() { if(g_initialised) CheckRails(); }   // faster rail reaction on the chart symbol's ticks

void OnTimer()
{
   if(!g_initialised) return;
   RollServerDay();
   CheckRails();
   if(PermHalted() || DayHalted()) return;
   for(int i = 0; i < N_SLEEVES; i++)
   {
      datetime b1 = iTime(S[i].sym, PERIOD_D1, 1);
      if(b1 > 0 && b1 != S[i].lastBar) OnNewBar(i);   // this symbol has a NEW closed bar
      ProcessPending(i);
   }
}
//+------------------------------------------------------------------+
