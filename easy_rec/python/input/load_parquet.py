import sys
import logging
import multiprocessing
import queue
import numpy as np
import pandas as pd

def start_data_proc(task_index, num_proc, file_que, data_que, proc_start_que, proc_stop_que,
    batch_size, label_fields, effective_fields, drop_remainder):
  mp_ctxt = multiprocessing.get_context('spawn')
  proc_arr = []
  for proc_id in range(num_proc):
    proc = mp_ctxt.Process(
        target=load_data_proc, args=(proc_id, file_que, data_que, proc_start_que,
            proc_stop_que, batch_size, label_fields, effective_fields,
            drop_remainder), name='task_%d_data_proc_%d' % (task_index, proc_id))
    proc.start()
    proc_arr.append(proc)
  return proc_arr

def _should_stop(proc_stop_que):
  try:
    proc_stop_que.get(block=False)
    logging.info('data_proc stop signal received')
    proc_stop_que.close()
    return True
  except queue.Empty:
    return False
  except ValueError:
    return True
  except AssertionError:
    return True

def _add_to_que(data_dict, data_que, proc_stop_que):
  while True:
    try:
      data_que.put(data_dict, timeout=5)
      return True
    except queue.Full:
      logging.warning('data_que is full')
      if _should_stop(proc_stop_que):
        return False
    except ValueError:
      logging.warning('data_que is closed')
      return False
    except AssertionError:
      logging.warning('data_que is closed')
      return False

def _get_one_file(file_que, proc_stop_que):
  if _should_stop(proc_stop_que):
    return None
  while True:
    try:
      input_file = file_que.get(block=False)
      return input_file
    except queue.Empty:
      if file_que.empty() and file_que.qsize() == 0:
        logging.info('file_que is empty: %d' % file_que.qsize())
        return None
  return input_file   

def load_data_proc(proc_id, file_que, data_que, proc_start_que, proc_stop_que,
   batch_size, label_fields, effective_fields, drop_remainder):
  logging.info('data proc %d start, proc_start_que=%s' % (proc_id, proc_start_que.qsize()))
  proc_start_que.get()
  all_fields = list(label_fields) + list(effective_fields)
  logging.info('data proc %d start, file_que.qsize=%d' % (proc_id, file_que.qsize()))
  num_files = 0
  part_data_dict = {}

  is_good = True
  while is_good:
    input_file = _get_one_file(file_que, proc_stop_que)
    if input_file is None:
      logging.info('input_file is none')
      is_good = False
      break
    num_files += 1
    input_data = pd.read_parquet(input_file, columns=all_fields)
    data_len = len(input_data[all_fields[0]])
    batch_num = int(data_len / batch_size)
    res_num = data_len % batch_size
    # logging.info(
    #     'proc[%d] read file %s sample_num=%d batch_num=%d res_num=%d' %
    #     (proc_id, input_file, data_len, batch_num, res_num))
    sid = 0
    for batch_id in range(batch_num):
      eid = sid + batch_size
      data_dict = {}
      for k in label_fields:
        data_dict[k] = np.array([x[0] for x in input_data[k][sid:eid]],
                                dtype=np.float32)
      for k in effective_fields:
        val = input_data[k][sid:eid]
        all_lens = np.array([len(x) for x in val], dtype=np.int32)
        all_vals = np.concatenate(list(val))
        assert np.sum(all_lens) == len(
            all_vals), 'len(all_vals)=%d np.sum(all_lens)=%d' % (
                len(all_vals), np.sum(all_lens))
        data_dict[k] = (all_lens, all_vals)

      fea_val_arr = [] 
      fea_len_arr = []
      for fea_name in effective_fields:
        fea_val_arr.append(data_dict[fea_name][1])
        fea_len_arr.append(data_dict[fea_name][0])
        del data_dict[fea_name]
      fea_lens = np.concatenate(fea_len_arr, axis=0)
      fea_vals = np.concatenate(fea_val_arr, axis=0)
      data_dict['feature'] = (fea_lens, fea_vals)
      if not _add_to_que(data_dict, data_que, proc_stop_que):
        logging.info('add to que failed')
        is_good = False
        break
      sid += batch_size
    if res_num > 0 and is_good:
      accum_res_num = 0
      data_dict = {}
      part_data_dict_n = {}
      for k in label_fields:
        tmp_lbls = np.array([x[0] for x in input_data[k][sid:]],
                                dtype=np.float32)
        if part_data_dict is not None and k in part_data_dict:
          tmp_lbls = np.concatenate([part_data_dict[k], tmp_lbls], axis=0)
          if len(tmp_lbls) > batch_size:
            data_dict[k] = tmp_lbls[:batch_size] 
            part_data_dict_n[k] = tmp_lbls[batch_size:]
          elif len(tmp_lbls) == batch_size:
            data_dict[k] = tmp_lbls
          else:
            part_data_dict_n[k] = tmp_lbls
        else:
          part_data_dict_n[k] = tmp_lbls
      for k in effective_fields:
        val = input_data[k][sid:]
        all_lens = np.array([len(x) for x in val], dtype=np.int32)
        all_vals = np.concatenate(list(val))
        if part_data_dict is not None and k in part_data_dict:
          tmp_lens = np.concatenate([part_data_dict[k][0], all_lens], axis=0)
          tmp_vals = np.concatenate([part_data_dict[k][1], all_vals], axis=0)
          if len(tmp_lens) > batch_size:
            tmp_res_lens = tmp_lens[batch_size:]
            tmp_lens = tmp_lens[:batch_size]
            tmp_num_elems = np.sum(tmp_lens)
            tmp_res_vals = tmp_vals[tmp_num_elems:]
            tmp_vals = tmp_vals[:tmp_num_elems]
            part_data_dict_n[k] = (tmp_res_lens, tmp_res_vals)
            data_dict[k] = (tmp_lens, tmp_vals)
          elif len(tmp_lens) == batch_size:
            data_dict[k] = (tmp_lens, tmp_vals)
          else:
            part_data_dict_n[k] = (tmp_lens, tmp_vals)
        else:
          part_data_dict_n[k] = (all_lens, all_vals)
      if len(data_dict) > 0:
        fea_val_arr = [] 
        fea_len_arr = []
        for fea_name in effective_fields:
          fea_val_arr.append(data_dict[fea_name][1])
          fea_len_arr.append(data_dict[fea_name][0])
          del data_dict[fea_name]
        fea_lens = np.concatenate(fea_len_arr, axis=0)
        fea_vals = np.concatenate(fea_val_arr, axis=0)
        data_dict['feature'] = (fea_lens, fea_vals)
        if not _add_to_que(data_dict, data_que, proc_stop_que):
          logging.info('add to que failed')
          is_good = False
          break

      part_data_dict = part_data_dict_n
  if len(part_data_dict) > 0 and is_good:
    if not drop_remainder:
      _add_to_que(part_data_dict, data_que, proc_stop_que)
    else:
      logging.warning('drop remain %d samples as drop_remainder is set' % \
           len(part_data_dict[label_fields[0]]))
  if is_good:
    is_good = _add_to_que(None, data_que, proc_stop_que)
  logging.info('data_proc_id=%d, is_good = %s' % (proc_id, is_good))
  data_que.close(wait_send_finish=is_good)

  if proc_id == 0:
    logging.info('data_que.qsize=%d' % data_que.qsize())
    while data_que.qsize() > 0:
      try:
        logging.info('data_que try to get one')
        data_que.get(timeout=5)
        logging.info('data_que get one')
      except queue.Empty:
        logging.warning('data_que.get timeout')
        pass
    logging.info('after clean: data_que.qsize=%d' % data_que.qsize())

  if proc_id == 0:
    while file_que.qsize() > 0:
      try:
        file_que.get(timeout=5)
      except queue.Empty:
        pass
    file_que.close()
  logging.info('data proc %d done, file_num=%d' % (proc_id, num_files))
